# Copyright 2009-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cursor class to iterate over Mongo query results."""
from __future__ import annotations

import copy
import warnings
from collections import deque
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Iterable,
    List,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    Union,
    cast,
    overload,
)

from bson import RE_TYPE, _convert_raw_document_lists_to_streams
from bson.code import Code
from bson.son import SON
from pymongo import _csot, helpers_shared
from pymongo.collation import validate_collation_or_none
from pymongo.common import (
    validate_is_document_type,
    validate_is_mapping,
)
from pymongo.cursor_shared import _CURSOR_CLOSED_ERRORS, _QUERY_OPTIONS, CursorType, _Hint, _Sort
from pymongo.errors import ConnectionFailure, InvalidOperation, OperationFailure
from pymongo.lock import _create_lock
from pymongo.message import (
    _CursorAddress,
    _GetMore,
    _OpMsg,
    _OpReply,
    _Query,
    _RawBatchGetMore,
    _RawBatchQuery,
)
from pymongo.response import PinnedResponse
from pymongo.synchronous.helpers import next
from pymongo.typings import _Address, _CollationIn, _DocumentOut, _DocumentType
from pymongo.write_concern import validate_boolean

if TYPE_CHECKING:
    from _typeshed import SupportsItems

    from bson.codec_options import CodecOptions
    from pymongo.read_preferences import _ServerMode
    from pymongo.synchronous.client_session import ClientSession
    from pymongo.synchronous.collection import Collection
    from pymongo.synchronous.pool import Connection

_IS_SYNC = True


class _ConnectionManager:
    """Used with exhaust cursors to ensure the connection is returned."""

    def __init__(self, conn: Connection, more_to_come: bool):
        self.conn: Optional[Connection] = conn
        self.more_to_come = more_to_come
        self._lock = _create_lock()

    def update_exhaust(self, more_to_come: bool) -> None:
        self.more_to_come = more_to_come

    def close(self) -> None:
        """Return this instance's connection to the connection pool."""
        if self.conn:
            self.conn.unpin()
            self.conn = None


class Cursor(Generic[_DocumentType]):
    _query_class = _Query
    _getmore_class = _GetMore

    def __init__(
        self,
        collection: Collection[_DocumentType],
        filter: Optional[Mapping[str, Any]] = None,
        projection: Optional[Union[Mapping[str, Any], Iterable[str]]] = None,
        skip: int = 0,
        limit: int = 0,
        no_cursor_timeout: bool = False,
        cursor_type: int = CursorType.NON_TAILABLE,
        sort: Optional[_Sort] = None,
        allow_partial_results: bool = False,
        oplog_replay: bool = False,
        batch_size: int = 0,
        collation: Optional[_CollationIn] = None,
        hint: Optional[_Hint] = None,
        max_scan: Optional[int] = None,
        max_time_ms: Optional[int] = None,
        max: Optional[_Sort] = None,
        min: Optional[_Sort] = None,
        return_key: Optional[bool] = None,
        show_record_id: Optional[bool] = None,
        snapshot: Optional[bool] = None,
        comment: Optional[Any] = None,
        session: Optional[ClientSession] = None,
        allow_disk_use: Optional[bool] = None,
        let: Optional[bool] = None,
    ) -> None:
        """Create a new cursor.

        Should not be called directly by application developers - see
        :meth:`~pymongo.collection.Collection.find` instead.

        .. seealso:: The MongoDB documentation on `cursors <https://dochub.mongodb.org/core/cursors>`_.
        """
        # Initialize all attributes used in __del__ before possibly raising
        # an error to avoid attribute errors during garbage collection.
        self._collection: Collection[_DocumentType] = collection
        self._id: Any = None
        self._exhaust = False
        self._sock_mgr: Any = None
        self._killed = False
        self._session: Optional[ClientSession]

        if session:
            self._session = session
            self._explicit_session = True
        else:
            self._session = None
            self._explicit_session = False

        spec: Mapping[str, Any] = filter or {}
        validate_is_mapping("filter", spec)
        if not isinstance(skip, int):
            raise TypeError(f"skip must be an instance of int, not {type(skip)}")
        if not isinstance(limit, int):
            raise TypeError(f"limit must be an instance of int, not {type(limit)}")
        validate_boolean("no_cursor_timeout", no_cursor_timeout)
        if no_cursor_timeout and not self._explicit_session:
            warnings.warn(
                "use an explicit session with no_cursor_timeout=True "
                "otherwise the cursor may still timeout after "
                "30 minutes, for more info see "
                "https://mongodb.com/docs/v4.4/reference/method/"
                "cursor.noCursorTimeout/"
                "#session-idle-timeout-overrides-nocursortimeout",
                UserWarning,
                stacklevel=2,
            )
        if cursor_type not in (
            CursorType.NON_TAILABLE,
            CursorType.TAILABLE,
            CursorType.TAILABLE_AWAIT,
            CursorType.EXHAUST,
        ):
            raise ValueError("not a valid value for cursor_type")
        validate_boolean("allow_partial_results", allow_partial_results)
        validate_boolean("oplog_replay", oplog_replay)
        if not isinstance(batch_size, int):
            raise TypeError(f"batch_size must be an integer, not {type(batch_size)}")
        if batch_size < 0:
            raise ValueError("batch_size must be >= 0")
        # Only set if allow_disk_use is provided by the user, else None.
        if allow_disk_use is not None:
            allow_disk_use = validate_boolean("allow_disk_use", allow_disk_use)

        if projection is not None:
            projection = helpers_shared._fields_list_to_dict(projection, "projection")

        if let is not None:
            validate_is_document_type("let", let)

        self._let = let
        self._spec = spec
        self._has_filter = filter is not None
        self._projection = projection
        self._skip = skip
        self._limit = limit
        self._batch_size = batch_size
        self._ordering = sort and helpers_shared._index_document(sort) or None
        self._max_scan = max_scan
        self._explain = False
        self._comment = comment
        self._max_time_ms = max_time_ms
        self._timeout = self._collection.database.client.options.timeout
        self._max_await_time_ms: Optional[int] = None
        self._max: Optional[Union[dict[Any, Any], _Sort]] = max
        self._min: Optional[Union[dict[Any, Any], _Sort]] = min
        self._collation = validate_collation_or_none(collation)
        self._return_key = return_key
        self._show_record_id = show_record_id
        self._allow_disk_use = allow_disk_use
        self._snapshot = snapshot
        self._hint: Union[str, dict[str, Any], None]
        self._set_hint(hint)

        # This is ugly. People want to be able to do cursor[5:5] and
        # get an empty result set (old behavior was an
        # exception). It's hard to do that right, though, because the
        # server uses limit(0) to mean 'no limit'. So we set __empty
        # in that case and check for it when iterating. We also unset
        # it anytime we change __limit.
        self._empty = False

        self._data: deque = deque()
        self._address: Optional[_Address] = None
        self._retrieved = 0

        self._codec_options = collection.codec_options
        # Read preference is set when the initial find is sent.
        self._read_preference: Optional[_ServerMode] = None
        self._read_concern = collection.read_concern

        self._query_flags = cursor_type
        self._cursor_type = cursor_type
        if no_cursor_timeout:
            self._query_flags |= _QUERY_OPTIONS["no_timeout"]
        if allow_partial_results:
            self._query_flags |= _QUERY_OPTIONS["partial"]
        if oplog_replay:
            self._query_flags |= _QUERY_OPTIONS["oplog_replay"]

        # The namespace to use for find/getMore commands.
        self._dbname = collection.database.name
        self._collname = collection.name

        # Checking exhaust cursor support requires network IO
        if _IS_SYNC:
            self._exhaust_checked = True
            self._supports_exhaust()  # type: ignore[unused-coroutine]
        else:
            self._exhaust = cursor_type == CursorType.EXHAUST
            self._exhaust_checked = False

    def _supports_exhaust(self) -> None:
        # Exhaust cursor support
        if self._cursor_type == CursorType.EXHAUST:
            if self._collection.database.client.is_mongos:
                raise InvalidOperation("Exhaust cursors are not supported by mongos")
            if self._limit:
                raise InvalidOperation("Can't use limit and exhaust together.")
            self._exhaust = True

    @property
    def collection(self) -> Collection[_DocumentType]:
        """The :class:`~pymongo.collection.Collection` that this
        :class:`Cursor` is iterating.
        """
        return self._collection

    @property
    def retrieved(self) -> int:
        """The number of documents retrieved so far."""
        return self._retrieved

    def __del__(self) -> None:
        self._die_no_lock()

    def clone(self) -> Cursor[_DocumentType]:
        """Get a clone of this cursor.

        Returns a new Cursor instance with options matching those that have
        been set on the current instance. The clone will be completely
        unevaluated, even if the current instance has been partially or
        completely evaluated.
        """
        return self._clone(True)

    def _clone(self, deepcopy: bool = True, base: Optional[Cursor] = None) -> Cursor:
        """Internal clone helper."""
        if not base:
            if self._explicit_session:
                base = self._clone_base(self._session)
            else:
                base = self._clone_base(None)

        values_to_clone = (
            "spec",
            "projection",
            "skip",
            "limit",
            "max_time_ms",
            "max_await_time_ms",
            "comment",
            "max",
            "min",
            "ordering",
            "explain",
            "hint",
            "batch_size",
            "max_scan",
            "query_flags",
            "collation",
            "empty",
            "show_record_id",
            "return_key",
            "allow_disk_use",
            "snapshot",
            "exhaust",
            "has_filter",
            "cursor_type",
        )
        data = {
            k: v for k, v in self.__dict__.items() if k.startswith("_") and k[1:] in values_to_clone
        }
        if deepcopy:
            data = self._deepcopy(data)
        base.__dict__.update(data)
        return base

    def _clone_base(self, session: Optional[ClientSession]) -> Cursor:
        """Creates an empty Cursor object for information to be copied into."""
        return self.__class__(self._collection, session=session)

    def _query_spec(self) -> Mapping[str, Any]:
        """Get the spec to use for a query."""
        operators: dict[str, Any] = {}
        if self._ordering:
            operators["$orderby"] = self._ordering
        if self._explain:
            operators["$explain"] = True
        if self._hint:
            operators["$hint"] = self._hint
        if self._let:
            operators["let"] = self._let
        if self._comment:
            operators["$comment"] = self._comment
        if self._max_scan:
            operators["$maxScan"] = self._max_scan
        if self._max_time_ms is not None:
            operators["$maxTimeMS"] = self._max_time_ms
        if self._max:
            operators["$max"] = self._max
        if self._min:
            operators["$min"] = self._min
        if self._return_key is not None:
            operators["$returnKey"] = self._return_key
        if self._show_record_id is not None:
            # This is upgraded to showRecordId for MongoDB 3.2+ "find" command.
            operators["$showDiskLoc"] = self._show_record_id
        if self._snapshot is not None:
            operators["$snapshot"] = self._snapshot

        if operators:
            # Make a shallow copy so we can cleanly rewind or clone.
            spec = dict(self._spec)

            # Allow-listed commands must be wrapped in $query.
            if "$query" not in spec:
                # $query has to come first
                spec = {"$query": spec}

            spec.update(operators)
            return spec
        # Have to wrap with $query if "query" is the first key.
        # We can't just use $query anytime "query" is a key as
        # that breaks commands like count and find_and_modify.
        # Checking spec.keys()[0] covers the case that the spec
        # was passed as an instance of SON or OrderedDict.
        elif "query" in self._spec and (len(self._spec) == 1 or next(iter(self._spec)) == "query"):
            return {"$query": self._spec}

        return self._spec

    def _check_okay_to_chain(self) -> None:
        """Check if it is okay to chain more options onto this cursor."""
        if self._retrieved or self._id is not None:
            raise InvalidOperation("cannot set options after executing query")

    def add_option(self, mask: int) -> Cursor[_DocumentType]:
        """Set arbitrary query flags using a bitmask.

        To set the tailable flag:
        cursor.add_option(2)
        """
        if not isinstance(mask, int):
            raise TypeError(f"mask must be an int, not {type(mask)}")
        self._check_okay_to_chain()

        if mask & _QUERY_OPTIONS["exhaust"]:
            if self._limit:
                raise InvalidOperation("Can't use limit and exhaust together.")
            if self._collection.database.client.is_mongos:
                raise InvalidOperation("Exhaust cursors are not supported by mongos")
            self._exhaust = True

        self._query_flags |= mask
        return self

    def remove_option(self, mask: int) -> Cursor[_DocumentType]:
        """Unset arbitrary query flags using a bitmask.

        To unset the tailable flag:
        cursor.remove_option(2)
        """
        if not isinstance(mask, int):
            raise TypeError(f"mask must be an int, not {type(mask)}")
        self._check_okay_to_chain()

        if mask & _QUERY_OPTIONS["exhaust"]:
            self._exhaust = False

        self._query_flags &= ~mask
        return self

    def allow_disk_use(self, allow_disk_use: bool) -> Cursor[_DocumentType]:
        """Specifies whether MongoDB can use temporary disk files while
        processing a blocking sort operation.

        Raises :exc:`TypeError` if `allow_disk_use` is not a boolean.

        .. note:: `allow_disk_use` requires server version **>= 4.4**

        :param allow_disk_use: if True, MongoDB may use temporary
            disk files to store data exceeding the system memory limit while
            processing a blocking sort operation.

        .. versionadded:: 3.11
        """
        if not isinstance(allow_disk_use, bool):
            raise TypeError(f"allow_disk_use must be a bool, not {type(allow_disk_use)}")
        self._check_okay_to_chain()

        self._allow_disk_use = allow_disk_use
        return self

    def limit(self, limit: int) -> Cursor[_DocumentType]:
        """Limits the number of results to be returned by this cursor.

        Raises :exc:`TypeError` if `limit` is not an integer. Raises
        :exc:`~pymongo.errors.InvalidOperation` if this :class:`Cursor`
        has already been used. The last `limit` applied to this cursor
        takes precedence. A limit of ``0`` is equivalent to no limit.

        :param limit: the number of results to return

        .. seealso:: The MongoDB documentation on `limit <https://dochub.mongodb.org/core/limit>`_.
        """
        if not isinstance(limit, int):
            raise TypeError(f"limit must be an integer, not {type(limit)}")
        if self._exhaust:
            raise InvalidOperation("Can't use limit and exhaust together.")
        self._check_okay_to_chain()

        self._empty = False
        self._limit = limit
        return self

    def batch_size(self, batch_size: int) -> Cursor[_DocumentType]:
        """Limits the number of documents returned in one batch. Each batch
        requires a round trip to the server. It can be adjusted to optimize
        performance and limit data transfer.

        .. note:: batch_size can not override MongoDB's internal limits on the
           amount of data it will return to the client in a single batch (i.e
           if you set batch size to 1,000,000,000, MongoDB will currently only
           return 4-16MB of results per batch).

        Raises :exc:`TypeError` if `batch_size` is not an integer.
        Raises :exc:`ValueError` if `batch_size` is less than ``0``.
        Raises :exc:`~pymongo.errors.InvalidOperation` if this
        :class:`Cursor` has already been used. The last `batch_size`
        applied to this cursor takes precedence.

        :param batch_size: The size of each batch of results requested.
        """
        if not isinstance(batch_size, int):
            raise TypeError(f"batch_size must be an integer, not {type(batch_size)}")
        if batch_size < 0:
            raise ValueError("batch_size must be >= 0")
        self._check_okay_to_chain()

        self._batch_size = batch_size
        return self

    def skip(self, skip: int) -> Cursor[_DocumentType]:
        """Skips the first `skip` results of this cursor.

        Raises :exc:`TypeError` if `skip` is not an integer. Raises
        :exc:`ValueError` if `skip` is less than ``0``. Raises
        :exc:`~pymongo.errors.InvalidOperation` if this :class:`Cursor` has
        already been used. The last `skip` applied to this cursor takes
        precedence.

        :param skip: the number of results to skip
        """
        if not isinstance(skip, int):
            raise TypeError(f"skip must be an integer, not {type(skip)}")
        if skip < 0:
            raise ValueError("skip must be >= 0")
        self._check_okay_to_chain()

        self._skip = skip
        return self

    def max_time_ms(self, max_time_ms: Optional[int]) -> Cursor[_DocumentType]:
        """Specifies a time limit for a query operation. If the specified
        time is exceeded, the operation will be aborted and
        :exc:`~pymongo.errors.ExecutionTimeout` is raised. If `max_time_ms`
        is ``None`` no limit is applied.

        Raises :exc:`TypeError` if `max_time_ms` is not an integer or ``None``.
        Raises :exc:`~pymongo.errors.InvalidOperation` if this :class:`Cursor`
        has already been used.

        :param max_time_ms: the time limit after which the operation is aborted
        """
        if not isinstance(max_time_ms, int) and max_time_ms is not None:
            raise TypeError(f"max_time_ms must be an integer or None, not {type(max_time_ms)}")
        self._check_okay_to_chain()

        self._max_time_ms = max_time_ms
        return self

    def max_await_time_ms(self, max_await_time_ms: Optional[int]) -> Cursor[_DocumentType]:
        """Specifies a time limit for a getMore operation on a
        :attr:`~pymongo.cursor.CursorType.TAILABLE_AWAIT` cursor. For all other
        types of cursor max_await_time_ms is ignored.

        Raises :exc:`TypeError` if `max_await_time_ms` is not an integer or
        ``None``. Raises :exc:`~pymongo.errors.InvalidOperation` if this
        :class:`Cursor` has already been used.

        .. note:: `max_await_time_ms` requires server version **>= 3.2**

        :param max_await_time_ms: the time limit after which the operation is
            aborted

        .. versionadded:: 3.2
        """
        if not isinstance(max_await_time_ms, int) and max_await_time_ms is not None:
            raise TypeError(
                f"max_await_time_ms must be an integer or None, not {type(max_await_time_ms)}"
            )
        self._check_okay_to_chain()

        # Ignore max_await_time_ms if not tailable or await_data is False.
        if self._query_flags & CursorType.TAILABLE_AWAIT:
            self._max_await_time_ms = max_await_time_ms

        return self

    @overload
    def __getitem__(self, index: int) -> _DocumentType:
        ...

    @overload
    def __getitem__(self, index: slice) -> Cursor[_DocumentType]:
        ...

    def __getitem__(self, index: Union[int, slice]) -> Union[_DocumentType, Cursor[_DocumentType]]:
        """Get a single document or a slice of documents from this cursor.

        .. warning:: A :class:`~Cursor` is not a Python :class:`list`. Each
          index access or slice requires that a new query be run using skip
          and limit. Do not iterate the cursor using index accesses.
          The following example is **extremely inefficient** and may return
          surprising results::

            cursor = db.collection.find()
            # Warning: This runs a new query for each document.
            # Don't do this!
            for idx in range(10):
                print(cursor[idx])

        Raises :class:`~pymongo.errors.InvalidOperation` if this
        cursor has already been used.

        To get a single document use an integral index, e.g.::

          >>> db.test.find()[50]

        An :class:`IndexError` will be raised if the index is negative
        or greater than the amount of documents in this cursor. Any
        limit previously applied to this cursor will be ignored.

        To get a slice of documents use a slice index, e.g.::

          >>> db.test.find()[20:25]

        This will return this cursor with a limit of ``5`` and skip of
        ``20`` applied.  Using a slice index will override any prior
        limits or skips applied to this cursor (including those
        applied through previous calls to this method). Raises
        :class:`IndexError` when the slice has a step, a negative
        start value, or a stop value less than or equal to the start
        value.

        :param index: An integer or slice index to be applied to this cursor
        """
        if _IS_SYNC:
            self._check_okay_to_chain()
            self._empty = False
            if isinstance(index, slice):
                if index.step is not None:
                    raise IndexError("Cursor instances do not support slice steps")

                skip = 0
                if index.start is not None:
                    if index.start < 0:
                        raise IndexError("Cursor instances do not support negative indices")
                    skip = index.start

                if index.stop is not None:
                    limit = index.stop - skip
                    if limit < 0:
                        raise IndexError(
                            "stop index must be greater than start index for slice %r" % index
                        )
                    if limit == 0:
                        self._empty = True
                else:
                    limit = 0

                self._skip = skip
                self._limit = limit
                return self

            if isinstance(index, int):
                if index < 0:
                    raise IndexError("Cursor instances do not support negative indices")
                clone = self.clone()
                clone.skip(index + self._skip)
                clone.limit(-1)  # use a hard limit
                clone._query_flags &= ~CursorType.TAILABLE_AWAIT  # PYTHON-1371
                for doc in clone:  # type: ignore[attr-defined]
                    return doc
                raise IndexError("no such item for Cursor instance")
            raise TypeError("index %r cannot be applied to Cursor instances" % index)
        else:
            raise IndexError("Cursor does not support indexing")

    def max_scan(self, max_scan: Optional[int]) -> Cursor[_DocumentType]:
        """**DEPRECATED** - Limit the number of documents to scan when
        performing the query.

        Raises :class:`~pymongo.errors.InvalidOperation` if this
        cursor has already been used. Only the last :meth:`max_scan`
        applied to this cursor has any effect.

        :param max_scan: the maximum number of documents to scan

        .. versionchanged:: 3.7
          Deprecated :meth:`max_scan`. Support for this option is deprecated in
          MongoDB 4.0. Use :meth:`max_time_ms` instead to limit server side
          execution time.
        """
        self._check_okay_to_chain()
        self._max_scan = max_scan
        return self

    def max(self, spec: _Sort) -> Cursor[_DocumentType]:
        """Adds ``max`` operator that specifies upper bound for specific index.

        When using ``max``, :meth:`~hint` should also be configured to ensure
        the query uses the expected index and starting in MongoDB 4.2
        :meth:`~hint` will be required.

        :param spec: a list of field, limit pairs specifying the exclusive
            upper bound for all keys of a specific index in order.

        .. versionchanged:: 3.8
           Deprecated cursors that use ``max`` without a :meth:`~hint`.

        .. versionadded:: 2.7
        """
        if not isinstance(spec, (list, tuple)):
            raise TypeError(f"spec must be an instance of list or tuple, not {type(spec)}")

        self._check_okay_to_chain()
        self._max = dict(spec)
        return self

    def min(self, spec: _Sort) -> Cursor[_DocumentType]:
        """Adds ``min`` operator that specifies lower bound for specific index.

        When using ``min``, :meth:`~hint` should also be configured to ensure
        the query uses the expected index and starting in MongoDB 4.2
        :meth:`~hint` will be required.

        :param spec: a list of field, limit pairs specifying the inclusive
            lower bound for all keys of a specific index in order.

        .. versionchanged:: 3.8
           Deprecated cursors that use ``min`` without a :meth:`~hint`.

        .. versionadded:: 2.7
        """
        if not isinstance(spec, (list, tuple)):
            raise TypeError(f"spec must be an instance of list or tuple, not {type(spec)}")

        self._check_okay_to_chain()
        self._min = dict(spec)
        return self

    def sort(
        self, key_or_list: _Hint, direction: Optional[Union[int, str]] = None
    ) -> Cursor[_DocumentType]:
        """Sorts this cursor's results.

        Pass a field name and a direction, either
        :data:`~pymongo.ASCENDING` or :data:`~pymongo.DESCENDING`.::

            for doc in collection.find().sort('field', pymongo.ASCENDING):
                print(doc)

        To sort by multiple fields, pass a list of (key, direction) pairs.
        If just a name is given, :data:`~pymongo.ASCENDING` will be inferred::

            for doc in collection.find().sort([
                    'field1',
                    ('field2', pymongo.DESCENDING)]):
                print(doc)

        Text search results can be sorted by relevance::

            cursor = db.test.find(
                {'$text': {'$search': 'some words'}},
                {'score': {'$meta': 'textScore'}})

            # Sort by 'score' field.
            cursor.sort([('score', {'$meta': 'textScore'})])

            for doc in cursor:
                print(doc)

        For more advanced text search functionality, see MongoDB's
        `Atlas Search <https://docs.atlas.mongodb.com/atlas-search/>`_.

        Raises :class:`~pymongo.errors.InvalidOperation` if this cursor has
        already been used. Only the last :meth:`sort` applied to this
        cursor has any effect.

        :param key_or_list: a single key or a list of (key, direction)
            pairs specifying the keys to sort on
        :param direction: only used if `key_or_list` is a single
            key, if not given :data:`~pymongo.ASCENDING` is assumed
        """
        self._check_okay_to_chain()
        keys = helpers_shared._index_list(key_or_list, direction)
        self._ordering = helpers_shared._index_document(keys)
        return self

    def explain(self) -> _DocumentType:
        """Returns an explain plan record for this cursor.

        .. note:: This method uses the default verbosity mode of the
          `explain command
          <https://mongodb.com/docs/manual/reference/command/explain/>`_,
          ``allPlansExecution``. To use a different verbosity use
          :meth:`~pymongo.database.Database.command` to run the explain
          command directly.

        .. note:: The timeout of this method can be set using :func:`pymongo.timeout`.

        .. seealso:: The MongoDB documentation on `explain <https://dochub.mongodb.org/core/explain>`_.
        """
        c = self.clone()
        c._explain = True

        # always use a hard limit for explains
        if c._limit:
            c._limit = -abs(c._limit)
        return next(c)

    def _set_hint(self, index: Optional[_Hint]) -> None:
        if index is None:
            self._hint = None
            return

        if isinstance(index, str):
            self._hint = index
        else:
            self._hint = helpers_shared._index_document(index)

    def hint(self, index: Optional[_Hint]) -> Cursor[_DocumentType]:
        """Adds a 'hint', telling Mongo the proper index to use for the query.

        Judicious use of hints can greatly improve query
        performance. When doing a query on multiple fields (at least
        one of which is indexed) pass the indexed field as a hint to
        the query. Raises :class:`~pymongo.errors.OperationFailure` if the
        provided hint requires an index that does not exist on this collection,
        and raises :class:`~pymongo.errors.InvalidOperation` if this cursor has
        already been used.

        `index` should be an index as passed to
        :meth:`~pymongo.collection.Collection.create_index`
        (e.g. ``[('field', ASCENDING)]``) or the name of the index.
        If `index` is ``None`` any existing hint for this query is
        cleared. The last hint applied to this cursor takes precedence
        over all others.

        :param index: index to hint on (as an index specifier)
        """
        self._check_okay_to_chain()
        self._set_hint(index)
        return self

    def comment(self, comment: Any) -> Cursor[_DocumentType]:
        """Adds a 'comment' to the cursor.

        http://mongodb.com/docs/manual/reference/operator/comment/

        :param comment: A string to attach to the query to help interpret and
            trace the operation in the server logs and in profile data.

        .. versionadded:: 2.7
        """
        self._check_okay_to_chain()
        self._comment = comment
        return self

    def where(self, code: Union[str, Code]) -> Cursor[_DocumentType]:
        """Adds a `$where`_ clause to this query.

        The `code` argument must be an instance of :class:`str` or
        :class:`~bson.code.Code` containing a JavaScript expression.
        This expression will be evaluated for each document scanned.
        Only those documents for which the expression evaluates to
        *true* will be returned as results. The keyword *this* refers
        to the object currently being scanned. For example::

            # Find all documents where field "a" is less than "b" plus "c".
            for doc in db.test.find().where('this.a < (this.b + this.c)'):
                print(doc)

        Raises :class:`TypeError` if `code` is not an instance of
        :class:`str`. Raises :class:`~pymongo.errors.InvalidOperation` if this
        :class:`Cursor` has already been used. Only the last call to
        :meth:`where` applied to a :class:`Cursor` has any effect.

        .. note:: MongoDB 4.4 drops support for :class:`~bson.code.Code`
          with scope variables. Consider using `$expr`_ instead.

        :param code: JavaScript expression to use as a filter

        .. _$expr: https://mongodb.com/docs/manual/reference/operator/query/expr/
        .. _$where: https://mongodb.com/docs/manual/reference/operator/query/where/
        """
        self._check_okay_to_chain()
        if not isinstance(code, Code):
            code = Code(code)

        # Avoid overwriting a filter argument that was given by the user
        # when updating the spec.
        spec: dict[str, Any]
        if self._has_filter:
            spec = dict(self._spec)
        else:
            spec = cast(dict, self._spec)
        spec["$where"] = code
        self._spec = spec
        return self

    def collation(self, collation: Optional[_CollationIn]) -> Cursor[_DocumentType]:
        """Adds a :class:`~pymongo.collation.Collation` to this query.

        Raises :exc:`TypeError` if `collation` is not an instance of
        :class:`~pymongo.collation.Collation` or a ``dict``. Raises
        :exc:`~pymongo.errors.InvalidOperation` if this :class:`Cursor` has
        already been used. Only the last collation applied to this cursor has
        any effect.

        :param collation: An instance of :class:`~pymongo.collation.Collation`.
        """
        self._check_okay_to_chain()
        self._collation = validate_collation_or_none(collation)
        return self

    def _unpack_response(
        self,
        response: Union[_OpReply, _OpMsg],
        cursor_id: Optional[int],
        codec_options: CodecOptions,
        user_fields: Optional[Mapping[str, Any]] = None,
        legacy_response: bool = False,
    ) -> Sequence[_DocumentOut]:
        return response.unpack_response(cursor_id, codec_options, user_fields, legacy_response)

    def _get_read_preference(self) -> _ServerMode:
        if self._read_preference is None:
            # Save the read preference for getMore commands.
            self._read_preference = self._collection._read_preference_for(self.session)
        return self._read_preference

    @property
    def alive(self) -> bool:
        """Does this cursor have the potential to return more data?

        This is mostly useful with `tailable cursors
        <https://www.mongodb.com/docs/manual/core/tailable-cursors/>`_
        since they will stop iterating even though they *may* return more
        results in the future.

        With regular cursors, simply use a for loop instead of :attr:`alive`::

            for doc in collection.find():
                print(doc)

        .. note:: Even if :attr:`alive` is True, :meth:`next` can raise
          :exc:`StopIteration`. :attr:`alive` can also be True while iterating
          a cursor from a failed server. In this case :attr:`alive` will
          return False after :meth:`next` fails to retrieve the next batch
          of results from the server.
        """
        return bool(len(self._data) or (not self._killed))

    @property
    def cursor_id(self) -> Optional[int]:
        """Returns the id of the cursor

        .. versionadded:: 2.2
        """
        return self._id

    @property
    def address(self) -> Optional[tuple[str, Any]]:
        """The (host, port) of the server used, or None.

        .. versionchanged:: 3.0
           Renamed from "conn_id".
        """
        return self._address

    @property
    def session(self) -> Optional[ClientSession]:
        """The cursor's :class:`~pymongo.client_session.ClientSession`, or None.

        .. versionadded:: 3.6
        """
        if self._explicit_session:
            return self._session
        return None

    def __copy__(self) -> Cursor[_DocumentType]:
        """Support function for `copy.copy()`.

        .. versionadded:: 2.4
        """
        return self._clone(deepcopy=False)

    def __deepcopy__(self, memo: Any) -> Any:
        """Support function for `copy.deepcopy()`.

        .. versionadded:: 2.4
        """
        return self._clone(deepcopy=True)

    @overload
    def _deepcopy(self, x: Iterable, memo: Optional[dict[int, Union[list, dict]]] = None) -> list:
        ...

    @overload
    def _deepcopy(
        self, x: SupportsItems, memo: Optional[dict[int, Union[list, dict]]] = None
    ) -> dict:
        ...

    def _deepcopy(
        self, x: Union[Iterable, SupportsItems], memo: Optional[dict[int, Union[list, dict]]] = None
    ) -> Union[list, dict]:
        """Deepcopy helper for the data dictionary or list.

        Regular expressions cannot be deep copied but as they are immutable we
        don't have to copy them when cloning.
        """
        y: Union[list, dict]
        iterator: Iterable[tuple[Any, Any]]
        if not hasattr(x, "items"):
            y, is_list, iterator = [], True, enumerate(x)
        else:
            y, is_list, iterator = {}, False, cast("SupportsItems", x).items()
        if memo is None:
            memo = {}
        val_id = id(x)
        if val_id in memo:
            return memo[val_id]
        memo[val_id] = y

        for key, value in iterator:
            if isinstance(value, (dict, list)) and not isinstance(value, SON):
                value = self._deepcopy(value, memo)  # noqa: PLW2901
            elif not isinstance(value, RE_TYPE):
                value = copy.deepcopy(value, memo)  # noqa: PLW2901

            if is_list:
                y.append(value)  # type: ignore[union-attr]
            else:
                if not isinstance(key, RE_TYPE):
                    key = copy.deepcopy(key, memo)  # noqa: PLW2901
                y[key] = value
        return y

    def _prepare_to_die(self, already_killed: bool) -> tuple[int, Optional[_CursorAddress]]:
        self._killed = True
        if self._id and not already_killed:
            cursor_id = self._id
            assert self._address is not None
            address = _CursorAddress(self._address, f"{self._dbname}.{self._collname}")
        else:
            # Skip killCursors.
            cursor_id = 0
            address = None
        return cursor_id, address

    def _die_no_lock(self) -> None:
        """Closes this cursor without acquiring a lock."""
        try:
            already_killed = self._killed
        except AttributeError:
            # ___init__ did not run to completion (or at all).
            return

        cursor_id, address = self._prepare_to_die(already_killed)
        self._collection.database.client._cleanup_cursor_no_lock(
            cursor_id, address, self._sock_mgr, self._session, self._explicit_session
        )
        if not self._explicit_session:
            self._session = None
        self._sock_mgr = None

    def _die_lock(self) -> None:
        """Closes this cursor."""
        try:
            already_killed = self._killed
        except AttributeError:
            # ___init__ did not run to completion (or at all).
            return

        cursor_id, address = self._prepare_to_die(already_killed)
        self._collection.database.client._cleanup_cursor_lock(
            cursor_id,
            address,
            self._sock_mgr,
            self._session,
            self._explicit_session,
        )
        if not self._explicit_session:
            self._session = None
        self._sock_mgr = None

    def close(self) -> None:
        """Explicitly close / kill this cursor."""
        self._die_lock()

    def distinct(self, key: str) -> list:
        """Get a list of distinct values for `key` among all documents
        in the result set of this query.

        Raises :class:`TypeError` if `key` is not an instance of
        :class:`str`.

        The :meth:`distinct` method obeys the
        :attr:`~pymongo.collection.Collection.read_preference` of the
        :class:`~pymongo.collection.Collection` instance on which
        :meth:`~pymongo.collection.Collection.find` was called.

        :param key: name of key for which we want to get the distinct values

        .. seealso:: :meth:`pymongo.collection.Collection.distinct`
        """
        options: dict[str, Any] = {}
        if self._spec:
            options["query"] = self._spec
        if self._max_time_ms is not None:
            options["maxTimeMS"] = self._max_time_ms
        if self._comment:
            options["comment"] = self._comment
        if self._collation is not None:
            options["collation"] = self._collation

        return self._collection.distinct(key, session=self._session, **options)

    def _send_message(self, operation: Union[_Query, _GetMore]) -> None:
        """Send a query or getmore operation and handles the response.

        If operation is ``None`` this is an exhaust cursor, which reads
        the next result batch off the exhaust socket instead of
        sending getMore messages to the server.

        Can raise ConnectionFailure.
        """
        client = self._collection.database.client
        # OP_MSG is required to support exhaust cursors with encryption.
        if client._encrypter and self._exhaust:
            raise InvalidOperation("exhaust cursors do not support auto encryption")

        try:
            response = client._run_operation(
                operation, self._unpack_response, address=self._address
            )
        except OperationFailure as exc:
            if exc.code in _CURSOR_CLOSED_ERRORS or self._exhaust:
                # Don't send killCursors because the cursor is already closed.
                self._killed = True
            if exc.timeout:
                self._die_no_lock()
            else:
                self.close()
            # If this is a tailable cursor the error is likely
            # due to capped collection roll over. Setting
            # self._killed to True ensures Cursor.alive will be
            # False. No need to re-raise.
            if (
                exc.code in _CURSOR_CLOSED_ERRORS
                and self._query_flags & _QUERY_OPTIONS["tailable_cursor"]
            ):
                return
            raise
        except ConnectionFailure:
            self._killed = True
            self.close()
            raise
        # Catch KeyboardInterrupt, CancelledError, etc. and cleanup.
        except BaseException:
            self.close()
            raise
        self._address = response.address
        if isinstance(response, PinnedResponse):
            if not self._sock_mgr:
                self._sock_mgr = _ConnectionManager(response.conn, response.more_to_come)  # type: ignore[arg-type]

        cmd_name = operation.name
        docs = response.docs
        if response.from_command:
            if cmd_name != "explain":
                cursor = docs[0]["cursor"]
                self._id = cursor["id"]
                if cmd_name == "find":
                    documents = cursor["firstBatch"]
                    # Update the namespace used for future getMore commands.
                    ns = cursor.get("ns")
                    if ns:
                        self._dbname, self._collname = ns.split(".", 1)
                else:
                    documents = cursor["nextBatch"]
                self._data = deque(documents)
                self._retrieved += len(documents)
            else:
                self._id = 0
                self._data = deque(docs)
                self._retrieved += len(docs)
        else:
            assert isinstance(response.data, _OpReply)
            self._id = response.data.cursor_id
            self._data = deque(docs)
            self._retrieved += response.data.number_returned

        if self._id == 0:
            # Don't wait for garbage collection to call __del__, return the
            # socket and the session to the pool now.
            self.close()

        if self._limit and self._id and self._limit <= self._retrieved:
            self.close()

    def _refresh(self) -> int:
        """Refreshes the cursor with more data from Mongo.

        Returns the length of self._data after refresh. Will exit early if
        self._data is already non-empty. Raises OperationFailure when the
        cursor cannot be refreshed due to an error on the query.
        """
        if len(self._data) or self._killed:
            return len(self._data)

        if not self._session:
            self._session = self._collection.database.client._ensure_session()

        if self._id is None:  # Query
            if (self._min or self._max) and not self._hint:
                raise InvalidOperation(
                    "Passing a 'hint' is required when using the min/max query"
                    " option to ensure the query utilizes the correct index"
                )
            q = self._query_class(
                self._query_flags,
                self._collection.database.name,
                self._collection.name,
                self._skip,
                self._query_spec(),
                self._projection,
                self._codec_options,
                self._get_read_preference(),
                self._limit,
                self._batch_size,
                self._read_concern,
                self._collation,
                self._session,
                self._collection.database.client,
                self._allow_disk_use,
                self._exhaust,
            )
            self._send_message(q)
        elif self._id:  # Get More
            if self._limit:
                limit = self._limit - self._retrieved
                if self._batch_size:
                    limit = min(limit, self._batch_size)
            else:
                limit = self._batch_size
            # Exhaust cursors don't send getMore messages.
            g = self._getmore_class(
                self._dbname,
                self._collname,
                limit,
                self._id,
                self._codec_options,
                self._get_read_preference(),
                self._session,
                self._collection.database.client,
                self._max_await_time_ms,
                self._sock_mgr,
                self._exhaust,
                self._comment,
            )
            self._send_message(g)

        return len(self._data)

    def rewind(self) -> Cursor[_DocumentType]:
        """Rewind this cursor to its unevaluated state.

        Reset this cursor if it has been partially or completely evaluated.
        Any options that are present on the cursor will remain in effect.
        Future iterating performed on this cursor will cause new queries to
        be sent to the server, even if the resultant data has already been
        retrieved by this cursor.
        """
        self.close()
        self._data = deque()
        self._id = None
        self._address = None
        self._retrieved = 0
        self._killed = False

        return self

    def next(self) -> _DocumentType:
        """Advance the cursor."""
        if not self._exhaust_checked:
            self._exhaust_checked = True
            self._supports_exhaust()
        if self._empty:
            raise StopIteration
        if len(self._data) or self._refresh():
            return self._data.popleft()
        else:
            raise StopIteration

    def _next_batch(self, result: list, total: Optional[int] = None) -> bool:
        """Get all or some documents from the cursor."""
        if not self._exhaust_checked:
            self._exhaust_checked = True
            self._supports_exhaust()
        if self._empty:
            return False
        if len(self._data) or self._refresh():
            if total is None:
                result.extend(self._data)
                self._data.clear()
            else:
                for _ in range(min(len(self._data), total)):
                    result.append(self._data.popleft())
            return True
        else:
            return False

    def __next__(self) -> _DocumentType:
        return self.next()

    def __iter__(self) -> Cursor[_DocumentType]:
        return self

    def __enter__(self) -> Cursor[_DocumentType]:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    @_csot.apply
    def to_list(self, length: Optional[int] = None) -> list[_DocumentType]:
        """Converts the contents of this cursor to a list more efficiently than ``[doc for doc in cursor]``.

        To use::

          >>> cursor.to_list()

        Or, to read at most n items from the cursor::

          >>> cursor.to_list(n)

        If the cursor is empty or has no more results, an empty list will be returned.

        .. versionadded:: 4.9
        """
        res: list[_DocumentType] = []
        remaining = length
        if isinstance(length, int) and length < 1:
            raise ValueError("to_list() length must be greater than 0")
        while self.alive:
            if not self._next_batch(res, remaining):
                break
            if length is not None:
                remaining = length - len(res)
                if remaining == 0:
                    break
        return res


class RawBatchCursor(Cursor, Generic[_DocumentType]):
    """A cursor / iterator over raw batches of BSON data from a query result."""

    _query_class = _RawBatchQuery
    _getmore_class = _RawBatchGetMore

    def __init__(self, collection: Collection[_DocumentType], *args: Any, **kwargs: Any) -> None:
        """Create a new cursor / iterator over raw batches of BSON data.

        Should not be called directly by application developers -
        see :meth:`~pymongo.collection.Collection.find_raw_batches`
        instead.

        .. seealso:: The MongoDB documentation on `cursors <https://dochub.mongodb.org/core/cursors>`_.
        """
        super().__init__(collection, *args, **kwargs)

    def _unpack_response(
        self,
        response: Union[_OpReply, _OpMsg],
        cursor_id: Optional[int],
        codec_options: CodecOptions[Mapping[str, Any]],
        user_fields: Optional[Mapping[str, Any]] = None,
        legacy_response: bool = False,
    ) -> list[_DocumentOut]:
        raw_response = response.raw_response(cursor_id, user_fields=user_fields)
        if not legacy_response:
            # OP_MSG returns firstBatch/nextBatch documents as a BSON array
            # Re-assemble the array of documents into a document stream
            _convert_raw_document_lists_to_streams(raw_response[0])
        return cast(List["_DocumentOut"], raw_response)

    def explain(self) -> _DocumentType:
        """Returns an explain plan record for this cursor.

        .. seealso:: The MongoDB documentation on `explain <https://dochub.mongodb.org/core/explain>`_.
        """
        clone = self._clone(deepcopy=True, base=Cursor(self.collection))
        return clone.explain()

    def __getitem__(self, index: Any) -> NoReturn:
        raise InvalidOperation("Cannot call __getitem__ on RawBatchCursor")
