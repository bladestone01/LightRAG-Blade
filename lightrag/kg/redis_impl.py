import asyncio
import os
import time
import traceback
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
MAX_CONNECTIONS = 1000
SOCKET_TIMEOUT = 10.0
SOCKET_CONNECT_TIMEOUT = 5.0
MAX_RETRIES = 3 #最大尝试次数
RETRY_DELAY = 0.5  # 重试间隔时间


@final
@dataclass
class RedisKVStorage(BaseKVStorage):
    def __post_init__(self):
        redis_uri = self.get_redis_config_url()
        logger.info(f"Loading config from local config: {redis_uri}")
        # Create a connection pool with limits
        self._pool = ConnectionPool.from_url(
            redis_uri,
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

    def get_redis_config_url(self):
        """
        获取Redis配置的URL
        Returns:
        """
        redis_uri = os.getenv("REDIS_URI")
        redis_host = os.getenv("REDIS_HOST")
        redis_port = os.getenv("REDIS_PORT")
        redis_db = os.getenv("REDIS_DB")
        redis_password = os.getenv("REDIS_PASSWORD")
        redis_username = os.getenv("REDIS_USERNAME")
        if redis_password:
            redis_uri = f"redis://{redis_username}:{redis_password}@{redis_host}:{redis_port}/{redis_db}"

        return redis_uri

    async def _reset_connection(self):
        """重置Redis连接池"""
        logger.warning("Resetting Redis connection pool...")
        try:
            # Close the old Redis client (closes all connections)
            if hasattr(self, "_redis") and self._redis:
                await self._redis.close()
            logger.info("Old Redis connection pool closed successfully")
        except Exception as e:
            logger.error(f"Error disconnecting old pool: {e}")

        # 创建新连接池
        self._pool = self._create_connection_pool()
        self._redis = Redis(connection_pool=self._pool)
        logger.info("Redis connection pool reset successfully")

    def _create_connection_pool(self):
        """创建Redis连接池"""
        logger.info("Recreating Redis connection pool...")
        redis_uri = self.get_redis_config_url()
        logger.info(f"Loading config from local config: {redis_uri}")
        # Create a connection pool with limits
        pool = ConnectionPool.from_url(
            redis_uri,
            # check the connection in an interval frequency
            health_check_interval=20,  # health check
            socket_keepalive=True,  # timeout retry
            retry_on_timeout=True,  # keep tcp connection
            max_connections=MAX_CONNECTIONS,
            decode_responses=True,
            socket_timeout=SOCKET_TIMEOUT,
            socket_connect_timeout=SOCKET_CONNECT_TIMEOUT,
        )
        logger.info(
            f"recreate Redis connection pool for {self.namespace} with max {MAX_CONNECTIONS} connections"
        )

        return pool


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
                    logger.error(traceback.format_exc())
                    raise

            except ConnectionError as e:
                logger.error(f"Redis connection error in {self.namespace}: {e}")
                logger.error(traceback.format_exc())
                # 立即尝试重置连接
                await self._reset_connection()
                retries += 1
                if retries >= MAX_RETRIES:
                    raise

            except AuthenticationError as e:
                logger.critical(f"Redis authentication failed: {e}")
                logger.error(traceback.format_exc())
                raise  # 认证错误无法恢复，直接抛出

            except RedisError as e:
                logger.error(f"Redis operation error in {self.namespace}: {e}")
                logger.error(traceback.format_exc())
                raise  # 其他Redis错误直接抛出

            except Exception as e:
                logger.error(
                    f"Unexpected error in Redis operation for {self.namespace}: {e}"
                )
                logger.error(traceback.format_exc())
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


    async def get_all(self, prefix:str="") -> list[dict[str, Any]]:
        """
          text_chunks:chunk-048c3eaf45947f4b3488c4f3312743fe
          从redis中获取所有数据，其中 key为text_chunks开头，结构示例为text_chunks:chunk-048c3eaf45947f4b3488c4f3312743fe）

          prefix:
             ""/default,  list all the data in redis
             "doc_id"/chunk_id list, 基于doc_id检索chunk list
             "chunk" : 基于chunk id检索chunk内容
        Returns:
        """
        logger.info(f"Getting all data in text chunks from {self.namespace}")
        async with self._get_redis_connection() as redis:
            try:
                keys = await redis.keys(f"{self.namespace}:{prefix}*")
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

                # 添加基于full_doc_id的反向索引，
                # text_chunk:doc_id:{full_doc_id} => Set of keys (e.g., text_chunk:1, text_chunk:2)
                # 在redis中, 保存一个set
                # 构建 full_doc_id 到 chunk_id 的映射
                doc_id_to_chunk_ids: dict[str, list[str]] = {}
                for k in data:
                    if "full_doc_id" in data[k]:
                        full_doc_id = data[k]["full_doc_id"]
                        chunk_key = f"{k}"
                        doc_id_to_chunk_ids.setdefault(full_doc_id, []).append(chunk_key)

                # 批量更新 Redis，将 list 转换为 JSON 字符串存储
                pipe = redis.pipeline()
                for full_doc_id, chunk_keys in doc_id_to_chunk_ids.items():
                    key_name = f"{self.namespace}:doc_id:{full_doc_id}"
                    # 获取现有值（如果存在）
                    existing_value = await redis.get(key_name)
                    existing_list = json.loads(existing_value) if existing_value else []

                    # 去重合并新旧数据
                    merged_list = list(set(existing_list + chunk_keys))
                    pipe.set(key_name, json.dumps(merged_list))

                await pipe.execute()

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

        logger.info(f"delete-action: Deleting {ids} entries from {self.namespace}")
        async with self._get_redis_connection() as redis:
            pipe = redis.pipeline()
            for id in ids:
                pipe.delete(f"{self.namespace}:{id}")

            results = await pipe.execute()
            deleted_count = sum(results)
            logger.info(
                f"Deleted {deleted_count} of {len(ids)} entries from {self.namespace}"
            )

    async def delete_by_doc_ids(self, doc_ids: list[str]) -> None:
        """Delete specific records from storage by their doc_ids
        """
        if not doc_ids:
            logger.info(f"未找到doc_ids: {doc_ids}")
            return

        start_time = time.time()
        logger.info(f"Deleting doc_ids from KV Storage, doc_ids:{doc_ids}")
        for doc_id in doc_ids:
            chunk_ids =  await self.get_by_id("doc_id:"+doc_id)
            logger .debug(f"Found {len(chunk_ids)} chunks to delete doc_id {doc_id}")
            if chunk_ids:
                await self.delete(chunk_ids)

        async with self._get_redis_connection() as redis:
            pipe = redis.pipeline()
            for id in doc_ids:
                pipe.delete(f"{self.namespace}:doc_id:{id}")
            await pipe.execute()
            logger.info(f"Deleted {len(doc_ids)} doc_ids from {self.namespace}")
        logger.info(f"End of Deleted Action: {len(doc_ids)} doc_ids from {self.namespace} cost {time.time()-start_time}s")




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
