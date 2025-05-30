import asyncio
import os
from typing import Any, final
from dataclasses import dataclass
import pipmaster as pm
import configparser
from contextlib import asynccontextmanager


if not pm.is_installed("redis"):
    pm.install("redis")

# aioredis is a depricated library, replaced with redis
from redis.asyncio import Redis, ConnectionPool  # type: ignore
from redis.exceptions import RedisError, ConnectionError  # type: ignore
from lightrag.utils import logger

from lightrag.base import BaseKVStorage
import json
from redis.exceptions import (
    ConnectionError,
    TimeoutError,
    RedisError,
    AuthenticationError
)


config = configparser.ConfigParser()
config.read("config.ini", "utf-8")

# Constants for Redis connection pool
MAX_CONNECTIONS = 50
SOCKET_TIMEOUT = 5.0
SOCKET_CONNECT_TIMEOUT = 3.0
MAX_RETRIES = 3 #最大尝试次数
RETRY_DELAY = 0.5  # 重试间隔时间


@final
@dataclass
class RedisKVStorage(BaseKVStorage):
    def __post_init__(self):
        redis_url = os.environ.get(
            "REDIS_URI", config.get("redis", "uri", fallback="redis://localhost:6379")
        )
        # Create a connection pool with limits
        self._pool = ConnectionPool.from_url(
            redis_url,
            #check the connection in an interval frequency
            health_check_interval=20, #health check
            socket_keepalive=True, # timeout retry
            retry_on_timeout=True, # keep tcp connection
            max_connections=MAX_CONNECTIONS,
            decode_responses=True,
            socket_timeout=SOCKET_TIMEOUT,
            socket_connect_timeout=SOCKET_CONNECT_TIMEOUT,
        )
        self._redis = Redis(connection_pool=self._pool)
        logger.info(
            f"Initialized Redis connection pool for {self.namespace} with max {MAX_CONNECTIONS} connections"
        )

    @asynccontextmanager
    async def _get_redis_connection(self):
        """安全上下文管理器，带自动重连机制"""
        retries = 0
        while retries < MAX_RETRIES:
            try:
                # 测试连接是否有效
                await self._redis.ping()
                yield self._redis
                return  # 成功则退出

            except TimeoutError as e:
                retries += 1
                logger.warning(
                    f"Redis timeout in {self.namespace} (attempt {retries}/{MAX_RETRIES}): {e}"
                )
                if retries < MAX_RETRIES:
                    # 指数退避策略
                    delay = RETRY_DELAY * (2 ** (retries - 1))
                    await asyncio.sleep(delay)
                    # 重置连接池
                    await self._reset_connection()
                else:
                    logger.error(
                        f"Max retries exceeded for Redis in {self.namespace}"
                    )
                    raise

            except ConnectionError as e:
                logger.error(f"Redis connection error in {self.namespace}: {e}")
                # 立即尝试重置连接
                await self._reset_connection()
                retries += 1
                if retries >= MAX_RETRIES:
                    raise

            except AuthenticationError as e:
                logger.critical(f"Redis authentication failed: {e}")
                raise  # 认证错误无法恢复，直接抛出

            except RedisError as e:
                logger.error(f"Redis operation error in {self.namespace}: {e}")
                raise  # 其他Redis错误直接抛出

            except Exception as e:
                logger.error(
                    f"Unexpected error in Redis operation for {self.namespace}: {e}"
                )
                raise

    async def close(self):
        """Close the Redis connection pool to prevent resource leaks."""
        if hasattr(self, "_redis") and self._redis:
            await self._redis.close()
            await self._pool.disconnect()
            logger.debug(f"Closed Redis connection pool for {self.namespace}")

    async def __aenter__(self):
        """Support for async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Ensure Redis resources are cleaned up when exiting context."""
        await self.close()


    async def get_all(self) -> list[dict[str, Any]]:
        """
          text_chunks:chunk-048c3eaf45947f4b3488c4f3312743fe
          从redis中获取所有数据，其中 key为text_chunks开头，结构示例为text_chunks:chunk-048c3eaf45947f4b3488c4f3312743fe）
        Returns:
        """
        logger.info(f"Getting all data in text chunks from {self.namespace}")
        async with self._get_redis_connection() as redis:
            try:
                keys = await redis.keys(f"{self.namespace}:*")
                if not keys:
                    logger.warn(f"未找到text_chunks开头的键: {self.namespace}")
                    return []

                pipe = redis.pipeline()
                for key in keys:
                    pipe.get(key)
                results = await pipe.execute()

                return {
                    key.split(":")[-1]: json.loads(result)
                    for key, result in zip(keys, results)
                    if result
                }
            except json.JSONDecodeError as e:
                logger.error(f"JSON解析错误: {e}")
                return []
            except Exception as e:
                logger.error(f"获取所有数据时发生错误: {e}")
                return []

    async def get_by_id(self, id: str) -> dict[str, Any] | None:
        async with self._get_redis_connection() as redis:
            try:
                data = await redis.get(f"{self.namespace}:{id}")
                return json.loads(data) if data else None
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error for id {id}: {e}")
                return None

    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        async with self._get_redis_connection() as redis:
            try:
                pipe = redis.pipeline()
                for id in ids:
                    pipe.get(f"{self.namespace}:{id}")
                results = await pipe.execute()
                return [json.loads(result) if result else None for result in results]
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error in batch get: {e}")
                return [None] * len(ids)

    async def filter_keys(self, keys: set[str]) -> set[str]:
        async with self._get_redis_connection() as redis:
            pipe = redis.pipeline()
            for key in keys:
                pipe.exists(f"{self.namespace}:{key}")
            results = await pipe.execute()

            existing_ids = {keys[i] for i, exists in enumerate(results) if exists}
            return set(keys) - existing_ids

    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        if not data:
            return

        logger.info(f"Inserting {len(data)} items to {self.namespace}")
        async with self._get_redis_connection() as redis:
            try:
                pipe = redis.pipeline()
                for k, v in data.items():
                    pipe.set(f"{self.namespace}:{k}", json.dumps(v))
                await pipe.execute()

                for k in data:
                    data[k]["_id"] = k
            except TypeError as e:
                logger.error(f"JSON encode type error during upsert: {e}")
                raise
            except ValueError as e:
                logger.error(f"JSON encode value error during upsert: {e}")
                raise
            except Exception as e:
                logger.error(f"JSON encode error during upsert: {e}")
                raise

    async def index_done_callback(self) -> None:
        # Redis handles persistence automatically
        pass

    async def delete(self, ids: list[str]) -> None:
        """Delete entries with specified IDs"""
        if not ids:
            return

        async with self._get_redis_connection() as redis:
            pipe = redis.pipeline()
            for id in ids:
                pipe.delete(f"{self.namespace}:{id}")

            results = await pipe.execute()
            deleted_count = sum(results)
            logger.info(
                f"Deleted {deleted_count} of {len(ids)} entries from {self.namespace}"
            )

    async def drop_cache_by_modes(self, modes: list[str] | None = None) -> bool:
        """Delete specific records from storage by by cache mode

        Importance notes for Redis storage:
        1. This will immediately delete the specified cache modes from Redis

        Args:
            modes (list[str]): List of cache mode to be drop from storage

        Returns:
             True: if the cache drop successfully
             False: if the cache drop failed
        """
        if not modes:
            return False

        try:
            await self.delete(modes)
            return True
        except Exception:
            return False

    async def drop(self) -> dict[str, str]:
        """Drop the storage by removing all keys under the current namespace.

        Returns:
            dict[str, str]: Status of the operation with keys 'status' and 'message'
        """
        async with self._get_redis_connection() as redis:
            try:
                keys = await redis.keys(f"{self.namespace}:*")

                if keys:
                    pipe = redis.pipeline()
                    for key in keys:
                        pipe.delete(key)
                    results = await pipe.execute()
                    deleted_count = sum(results)

                    logger.info(f"Dropped {deleted_count} keys from {self.namespace}")
                    return {
                        "status": "success",
                        "message": f"{deleted_count} keys dropped",
                    }
                else:
                    logger.info(f"No keys found to drop in {self.namespace}")
                    return {"status": "success", "message": "no keys to drop"}

            except Exception as e:
                logger.error(f"Error dropping keys from {self.namespace}: {e}")
                return {"status": "error", "message": str(e)}
