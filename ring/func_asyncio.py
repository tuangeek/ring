""":mod:`ring.func_asyncio` --- collection of :mod:`asyncio` factory functions.

This module includes building blocks and storage implementations of **Ring**
factories for :mod:`asyncio`.
"""
from typing import Any, Optional, List
import asyncio
import inspect
import itertools
import functools
from . import func_base as fbase, func_sync as fsync

__all__ = ('aiomcache', 'aioredis', )

inspect_iscoroutinefunction = getattr(
    inspect, 'iscoroutinefunction', lambda f: False)


def convert_storage(storage_class):
    storage_bases = (fbase.CommonMixinStorage, BulkStorageMixin)
    async_storage_class = type(
        'Async' + storage_class.__name__, (storage_class,), {})

    count = 0
    for storage_base in storage_bases:
        if issubclass(storage_class, storage_base):
            count += 1
            for name in storage_base.__dict__.keys():
                async_attr = asyncio.coroutine(getattr(storage_class, name))
                setattr(async_storage_class, name, async_attr)
    if count == 0:
        raise TypeError(
            "'storage_class' is not subclassing any known storage base")

    return async_storage_class


class NonAsyncioFactoryProxyBase(fbase.FactoryProxyBase):

    def __init__(self, *args, **kwargs):
        self.force_asyncio = kwargs.pop('force_asyncio', False)
        super().__init__(*args, **kwargs)

    def __call__(self, func):
        is_coroutine = fbase.asyncio_binary_classifier(func) == 1
        if is_coroutine and not self.force_asyncio:
            raise TypeError(
                "'{f.__name__}' function is a asyncio coroutine but the ring "
                "factory does not support asyncio. This may result the "
                "storage operation blocking asyncio event loop which may "
                "slows down the program. To force to allow it, pass "
                "keyword parameter 'force_asyncio=True' to the ring factory.")
        return super().__call__(func)


def create_factory_proxy(factory_table, *, allow_asyncio):
    if allow_asyncio:
        proxy_base = fbase.FactoryProxyBase
    else:
        proxy_base = NonAsyncioFactoryProxyBase
    classifier = fbase.asyncio_binary_classifier
    proxy_class = type('_FactoryProxy', (proxy_base,), {})
    proxy_class.classifier = staticmethod(classifier)
    proxy_class.factory_table = staticmethod(factory_table)
    proxy_class.__call__ = functools.wraps(factory_table[0])(proxy_class.__call__)
    return proxy_class


def create_from(_storage_class):

    def factory(
            obj, key_prefix=None, expire=None, coder=None, ignorable_keys=None,
            user_interface=CacheUserInterface, storage_class=None,
            **kwargs):

        if storage_class is None:
            storage_class = convert_storage(_storage_class)

        return fbase.factory(
            obj, key_prefix=key_prefix, on_manufactured=factory_doctor,
            user_interface=user_interface,
            storage_class=convert_storage(storage_class),
            miss_value=None, expire_default=expire, coder=coder,
            ignorable_keys=ignorable_keys,
            **kwargs)

    return factory


def factory_doctor(wire_rope) -> None:
    callable = wire_rope.callable
    if not callable.is_coroutine:
        raise TypeError(
            "The function for cache '{}' must be an async function.".format(
                callable.code.co_name))


class CommonMixinStorage(fbase.BaseStorage):  # Working only as mixin
    """General :mod:`asyncio` storage root for BaseStorageMixin."""

    @asyncio.coroutine
    def get(self, key):
        value = yield from self.get_value(key)
        return self.ring.coder.decode(value)

    @asyncio.coroutine
    def set(self, key, value, expire=...):
        if expire is ...:
            expire = self.ring.expire_default
        encoded = self.ring.coder.encode(value)
        result = yield from self.set_value(key, encoded, expire)
        return result

    @asyncio.coroutine
    def delete(self, key):
        result = yield from self.delete_value(key)
        return result

    @asyncio.coroutine
    def has(self, key):
        result = yield from self.has_value(key)
        return result

    @asyncio.coroutine
    def touch(self, key, expire=...):
        if expire is ...:
            expire = self.ring.expire_default
        result = yield from self.touch_value(key, expire)
        return result


class CacheUserInterface(fbase.BaseUserInterface):
    """General cache user interface provider for :mod:`asyncio`.

    :see: :class:`ring.func_base.BaseUserInterface` for class and methods
        details.
    """

    @fbase.interface_attrs(
        transform_args=fbase.transform_kwargs_only,
        return_annotation=lambda a: Optional[a.get('return', Any)])
    @asyncio.coroutine
    def get(self, wire, **kwargs):
        key = self.key(wire, **kwargs)
        try:
            result = yield from self.ring.storage.get(key)
        except fbase.NotFound:
            result = self.ring.miss_value
        return result

    @fbase.interface_attrs(transform_args=fbase.transform_kwargs_only)
    @asyncio.coroutine
    def update(self, wire, **kwargs):
        key = self.key(wire, **kwargs)
        result = yield from self.execute(wire, **kwargs)
        yield from self.ring.storage.set(key, result)
        return result

    @fbase.interface_attrs(transform_args=fbase.transform_kwargs_only)
    @asyncio.coroutine
    def get_or_update(self, wire, **kwargs):
        key = self.key(wire, **kwargs)
        try:
            result = yield from self.ring.storage.get(key)
        except fbase.NotFound:
            result = yield from self.execute(wire, **kwargs)
            yield from self.ring.storage.set(key, result)
        return result

    @fbase.interface_attrs(
        transform_args=(fbase.transform_kwargs_only, {'prefix_count': 1}),
        return_annotation=None)
    def set(self, wire, _value, **kwargs):
        key = self.key(wire, **kwargs)
        return self.ring.storage.set(key, _value)

    @fbase.interface_attrs(
        transform_args=fbase.transform_kwargs_only, return_annotation=None)
    def delete(self, wire, **kwargs):
        key = self.key(wire, **kwargs)
        return self.ring.storage.delete(key)

    @fbase.interface_attrs(
        transform_args=fbase.transform_kwargs_only, return_annotation=bool)
    def has(self, wire, **kwargs):
        key = self.key(wire, **kwargs)
        return self.ring.storage.has(key)

    @fbase.interface_attrs(
        transform_args=fbase.transform_kwargs_only, return_annotation=None)
    def touch(self, wire, **kwargs):
        key = self.key(wire, **kwargs)
        return self.ring.storage.touch(key)


class BulkInterfaceMixin(fbase.AbstractBulkUserInterfaceMixin):
    """Bulk access interface mixin.

    Any corresponding storage class must be a subclass of
    :class:`ring.func_asyncio.BulkStorageMixin`.
    """

    @fbase.interface_attrs(
        return_annotation=lambda a: List[a.get('return', Any)])
    def execute_many(self, wire, *args_list):
        return asyncio.gather(*(
            fbase.execute_bulk_item(wire, args) for args in args_list))

    @fbase.interface_attrs(
        return_annotation=lambda a: List[Optional[a.get('return', Any)]])
    def get_many(self, wire, *args_list):
        keys = self.key_many(wire, *args_list)
        return self.ring.storage.get_many(
            keys, miss_value=self.ring.miss_value)

    @fbase.interface_attrs(
        return_annotation=lambda a: List[a.get('return', Any)])
    @asyncio.coroutine
    def update_many(self, wire, *args_list):
        keys = self.key_many(wire, *args_list)
        values = yield from self.execute_many(wire, *args_list)
        yield from self.ring.storage.set_many(keys, values)
        return values

    @fbase.interface_attrs(
        return_annotation=lambda a: List[a.get('return', Any)])
    @asyncio.coroutine
    def get_or_update_many(self, wire, *args_list):
        keys = self.key_many(wire, *args_list)
        miss_value = object()
        results = yield from self.ring.storage.get_many(
            keys, miss_value=miss_value)

        miss_indices = []
        for i, akr in enumerate(zip(args_list, keys, results)):
            args, key, result = akr
            if result is not miss_value:
                continue
            miss_indices.append(i)

        new_results = yield from asyncio.gather(*(
            fbase.execute_bulk_item(wire, args_list[i]) for i in miss_indices))
        new_keys = [keys[i] for i in miss_indices]
        yield from self.ring.storage.set_many(new_keys, new_results)

        for new_i, old_i in enumerate(miss_indices):
            results[old_i] = new_results[new_i]
        return results

    @fbase.interface_attrs(return_annotation=None)
    def set_many(self, wire, args_list, value_list):
        keys = self.key_many(wire, *args_list)
        return self.ring.storage.set_many(keys, value_list)

    @fbase.interface_attrs(return_annotation=None)
    def delete_many(self, wire, *args_list):
        keys = self.key_many(wire, *args_list)
        return self.ring.storage.delete_many(keys)

    @fbase.interface_attrs(return_annotation=None)
    def has_many(self, wire, *args_list):
        keys = self.key_many(wire, *args_list)
        return self.ring.storage.has_many(keys)

    @fbase.interface_attrs(return_annotation=None)
    def touch_many(self, wire, *args_list):
        keys = self.key_many(wire, *args_list)
        return self.ring.storage.touch_many(keys)


class BulkStorageMixin(object):

    @asyncio.coroutine
    def get_many(self, keys, miss_value):
        """Get and return values for the given key."""
        values = yield from self.get_many_values(keys)
        results = [
            self.ring.coder.decode(v) if v is not fbase.NotFound else miss_value  # noqa
            for v in values]
        return results

    def set_many(self, keys, values, expire=Ellipsis):
        """Set values for the given keys."""
        if expire is Ellipsis:
            expire = self.ring.expire_default
        return self.set_many_values(
            keys, [self.ring.coder.encode(v) for v in values], expire)

    def delete_many(self, keys):
        """Delete values for the given keys."""
        return self.delete_many_values(keys)

    def has_many(self, keys):
        """Check and return existences for the given keys."""
        return self.has_many_values(keys)

    def touch_many(self, keys, expire=Ellipsis):
        """Touch values for the given keys."""
        if expire is Ellipsis:
            expire = self.ring.expire_default
        return self.touch_many_values(keys, expire)


class AiomcacheStorage(
        CommonMixinStorage, fbase.StorageMixin, BulkStorageMixin):
    """Storage implementation for :class:`aiomcache.Client`."""

    @asyncio.coroutine
    def get_value(self, key):
        value = yield from self.backend.get(key)
        if value is None:
            raise fbase.NotFound
        return value

    def set_value(self, key, value, expire):
        return self.backend.set(key, value, expire)

    def delete_value(self, key):
        return self.backend.delete(key)

    def touch_value(self, key, expire):
        return self.backend.touch(key, expire)

    @asyncio.coroutine
    def get_many_values(self, keys):
        values = yield from self.backend.multi_get(*keys)
        return [v if v is not None else fbase.NotFound for v in values]

    def set_many_values(self, keys, values, expire):
        raise NotImplementedError("aiomcache doesn't support set_multi.")

    def delete_many_values(self, keys):
        raise NotImplementedError("aiomcache doesn't support delete_multi.")


class AioredisStorage(
        CommonMixinStorage, fbase.StorageMixin, BulkStorageMixin):
    """Storage implementation for :class:`aioredis.Redis`."""

    @asyncio.coroutine
    def get_value(self, key):
        value = yield from self.backend.get(key)
        if value is None:
            raise fbase.NotFound
        return value

    def set_value(self, key, value, expire):
        return self.backend.set(key, value, expire=expire)

    def delete_value(self, key):
        return self.backend.delete(key)

    @asyncio.coroutine
    def has_value(self, key):
        result = yield from self.backend.exists(key)
        return bool(result)

    def touch_value(self, key, expire):
        if expire is None:
            raise TypeError("'touch' is requested for persistent cache")
        return self.backend.expire(key, expire)

    @asyncio.coroutine
    def get_many_values(self, keys):
        values = yield from self.backend.mget(*keys)
        return [v if v is not None else fbase.NotFound for v in values]

    @asyncio.coroutine
    def set_many_values(self, keys, values, expire):
        params = itertools.chain.from_iterable(zip(keys, values))
        yield from self.backend.mset(*params)
        if expire is not None:
            asyncio.ensure_future(asyncio.gather(*(
                self.backend.expire(key, expire) for key in keys)))


def dict(
        obj, key_prefix=None, expire=None, coder=None, ignorable_keys=None,
        user_interface=CacheUserInterface, storage_class=None,
        **kwargs):

    if storage_class is None:
        if expire is None:
            storage_class = fsync.PersistentDictStorage
        else:
            storage_class = fsync.ExpirableDictStorage

    return fbase.factory(
        obj, key_prefix=key_prefix, on_manufactured=None,
        user_interface=user_interface,
        storage_class=convert_storage(storage_class),
        miss_value=None, expire_default=expire, coder=coder,
        ignorable_keys=ignorable_keys,
        **kwargs)


def aiomcache(
        client, key_prefix=None, expire=0, coder=None, ignorable_keys=None,
        user_interface=(CacheUserInterface, BulkInterfaceMixin),
        storage_class=AiomcacheStorage, key_encoding='utf-8',
        **kwargs):
    """Memcached_ interface for :mod:`asyncio`.

    Expected client package is aiomcache_.

    aiomcache expect `Memcached` client or dev package is installed on your
    machine. If you are new to Memcached, check how to install it and the
    python package on your platform.

    :param aiomcache.Client client: aiomcache client object.
    :param object key_refactor: The default key refactor may hash the cache
        key when it doesn't meet memcached key restriction.

    :see: :func:`ring.func_asyncio.CacheUserInterface` for single access
        sub-functions.
    :see: :func:`ring.func_asyncio.BulkInterfaceMixin` for bulk access
        sub-functions.

    :see: :func:`ring.memcache` for non-asyncio version.

    .. _Memcache: http://memcached.org/
    .. _aiomcache: https://pypi.org/project/aiomcache/
    """
    from ring._memcache import key_refactor

    return fbase.factory(
        client, key_prefix=key_prefix, on_manufactured=factory_doctor,
        user_interface=user_interface, storage_class=storage_class,
        miss_value=None, expire_default=expire, coder=coder,
        ignorable_keys=ignorable_keys,
        key_encoding=key_encoding,
        key_refactor=key_refactor,
        **kwargs)


def aioredis(
        redis, key_prefix=None, expire=None, coder=None, ignorable_keys=None,
        user_interface=(CacheUserInterface, BulkInterfaceMixin),
        storage_class=AioredisStorage,
        **kwargs):
    """Redis interface for :mod:`asyncio`.

    Expected client package is aioredis_.

    aioredis expect `Redis` client or dev package is installed on your
    machine. If you are new to Memcached, check how to install it and the
    python package on your platform.

    Note that aioredis>=1.0.0 only supported.

    .. _Redis: http://redis.io/
    .. _aioredis: https://pypi.org/project/aioredis/

    :param aioredis.Redis client: aioredis interface object. See
        :func:`aioredis.create_redis` or :func:`aioredis.create_redis_pool`.

    :see: :func:`ring.func_asyncio.CacheUserInterface` for single access
        sub-functions.
    :see: :func:`ring.func_asyncio.BulkInterfaceMixin` for bulk access
        sub-functions.

    :see: :func:`ring.redis` for non-asyncio version.
    """
    return fbase.factory(
        redis, key_prefix=key_prefix, on_manufactured=factory_doctor,
        user_interface=user_interface, storage_class=storage_class,
        miss_value=None, expire_default=expire, coder=coder,
        ignorable_keys=ignorable_keys,
        **kwargs)
