import asyncio
import json
import os
import datetime
from datetime import timezone
from dataclasses import dataclass, field
from typing import Any, Union, final
import numpy as np
import configparser
from ..prompt import PROMPTS, GRAPH_FIELD_SEP
import time

from lightrag.types import KnowledgeGraph, KnowledgeGraphNode, KnowledgeGraphEdge

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..base import (
    BaseGraphStorage,
    BaseKVStorage,
    BaseVectorStorage,
    DocProcessingStatus,
    DocStatus,
    DocStatusStorage,
)
from ..namespace import NameSpace, is_namespace
from ..utils import logger

import pipmaster as pm

if not pm.is_installed("asyncpg"):
    pm.install("asyncpg")

import asyncpg  # type: ignore
from asyncpg import Pool  # type: ignore

from dotenv import load_dotenv

# use the .env that is inside the current folder
# allows to use different .env file for each lightrag instance
# the OS environment variables take precedence over the .env file
load_dotenv(dotenv_path=".env", override=False)

# Get maximum number of graph nodes from environment variable, default is 1000
MAX_GRAPH_NODES = int(os.getenv("MAX_GRAPH_NODES", 1000))


class PostgreSQLDB:
    def __init__(self, config: dict[str, Any], **kwargs: Any):
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 5432)
        self.user = config.get("user", "postgres")
        self.password = config.get("password", None)
        self.database = config.get("database", "postgres")
        self.workspace = config.get("workspace", "default")
        self.max = int(config.get("max_connections", 12))
        self.increment = 1
        self.pool: Pool | None = None

        if self.user is None or self.password is None or self.database is None:
            raise ValueError("Missing database user, password, or database")

    async def initdb(self):
        try:
            self.pool = await asyncpg.create_pool(  # type: ignore
                user=self.user,
                password=self.password,
                database=self.database,
                host=self.host,
                port=self.port,
                min_size=1,
                max_size=self.max,
            )

            logger.info(
                f"PostgreSQL, Connected to database at {self.host}:{self.port}/{self.database}"
            )
        except Exception as e:
            logger.error(
                f"PostgreSQL, Failed to connect database at {self.host}:{self.port}/{self.database}, Got:{e}"
            )
            raise

    @staticmethod
    async def configure_age(connection: asyncpg.Connection, graph_name: str) -> None:
        """Set the Apache AGE environment and creates a graph if it does not exist.

        This method:
        - Sets the PostgreSQL `search_path` to include `ag_catalog`, ensuring that Apache AGE functions can be used without specifying the schema.
        - Attempts to create a new graph with the provided `graph_name` if it does not already exist.
        - Silently ignores errors related to the graph already existing.

        """
        try:
            await connection.execute(  # type: ignore
                'SET search_path = ag_catalog, "$user", public'
            )
            await connection.execute(  # type: ignore
                f"select create_graph('{graph_name}')"
            )
        except (
            asyncpg.exceptions.InvalidSchemaNameError,
            asyncpg.exceptions.UniqueViolationError,
        ):
            pass

    async def _migrate_timestamp_columns(self):
        """Migrate timestamp columns in tables to timezone-aware types, assuming original data is in UTC time"""
        # Tables and columns that need migration
        tables_to_migrate = {
            "LIGHTRAG_VDB_ENTITY": ["create_time", "update_time"],
            "LIGHTRAG_VDB_RELATION": ["create_time", "update_time"],
            "LIGHTRAG_DOC_CHUNKS": ["create_time", "update_time"],
        }

        for table_name, columns in tables_to_migrate.items():
            for column_name in columns:
                try:
                    # Check if column exists
                    check_column_sql = f"""
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_name = '{table_name.lower()}'
                    AND column_name = '{column_name}'
                    """

                    column_info = await self.query(check_column_sql)
                    if not column_info:
                        logger.warning(
                            f"Column {table_name}.{column_name} does not exist, skipping migration"
                        )
                        continue

                    # Check column type
                    data_type = column_info.get("data_type")
                    if data_type == "timestamp with time zone":
                        logger.info(
                            f"Column {table_name}.{column_name} is already timezone-aware, no migration needed"
                        )
                        continue

                    # Execute migration, explicitly specifying UTC timezone for interpreting original data
                    logger.info(
                        f"Migrating {table_name}.{column_name} to timezone-aware type"
                    )
                    migration_sql = f"""
                    ALTER TABLE {table_name}
                    ALTER COLUMN {column_name} TYPE TIMESTAMP(0) WITH TIME ZONE
                    USING {column_name} AT TIME ZONE 'UTC'
                    """

                    await self.execute(migration_sql)
                    logger.info(
                        f"Successfully migrated {table_name}.{column_name} to timezone-aware type"
                    )
                except Exception as e:
                    # Log error but don't interrupt the process
                    logger.warning(f"Failed to migrate {table_name}.{column_name}: {e}")

    async def check_tables(self):
        # First create all tables
        for k, v in TABLES.items():
            try:
                await self.query(f"SELECT 1 FROM {k} LIMIT 1")
            except Exception:
                try:
                    logger.info(f"PostgreSQL, Try Creating table {k} in database")
                    await self.execute(v["ddl"])
                    logger.info(
                        f"PostgreSQL, Creation success table {k} in PostgreSQL database"
                    )
                except Exception as e:
                    logger.error(
                        f"PostgreSQL, Failed to create table {k} in database, Please verify the connection with PostgreSQL database, Got: {e}"
                    )
                    raise e

            # Create index for id column in each table
            try:
                index_name = f"idx_{k.lower()}_id"
                check_index_sql = f"""
                SELECT 1 FROM pg_indexes
                WHERE indexname = '{index_name}'
                AND tablename = '{k.lower()}'
                """
                index_exists = await self.query(check_index_sql)

                if not index_exists:
                    create_index_sql = f"CREATE INDEX {index_name} ON {k}(id)"
                    logger.info(f"PostgreSQL, Creating index {index_name} on table {k}")
                    await self.execute(create_index_sql)
            except Exception as e:
                logger.error(
                    f"PostgreSQL, Failed to create index on table {k}, Got: {e}"
                )

        # After all tables are created, attempt to migrate timestamp fields
        try:
            await self._migrate_timestamp_columns()
        except Exception as e:
            logger.error(f"PostgreSQL, Failed to migrate timestamp columns: {e}")
            # Don't throw an exception, allow the initialization process to continue

    async def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        multirows: bool = False,
        with_age: bool = False,
        graph_name: str | None = None,
    ) -> dict[str, Any] | None | list[dict[str, Any]]:
        # start_time = time.time()
        # logger.info(f"PostgreSQL, Querying:\n{sql}")

        async with self.pool.acquire() as connection:  # type: ignore
            if with_age and graph_name:
                await self.configure_age(connection, graph_name)  # type: ignore
            elif with_age and not graph_name:
                raise ValueError("Graph name is required when with_age is True")

            try:
                if params:
                    rows = await connection.fetch(sql, *params.values())
                else:
                    rows = await connection.fetch(sql)

                if multirows:
                    if rows:
                        columns = [col for col in rows[0].keys()]
                        data = [dict(zip(columns, row)) for row in rows]
                    else:
                        data = []
                else:
                    if rows:
                        columns = rows[0].keys()
                        data = dict(zip(columns, rows[0]))
                    else:
                        data = None

                # query_time = time.time() - start_time
                # logger.info(f"PostgreSQL, Query result len: {len(data)}")
                # logger.info(f"PostgreSQL, Query execution time: {query_time:.4f}s")

                return data
            except Exception as e:
                logger.error(f"PostgreSQL database, error:{e}")
                raise

    async def execute(
        self,
        sql: str,
        data: dict[str, Any] | None = None,
        upsert: bool = False,
        with_age: bool = False,
        graph_name: str | None = None,
    ):
        try:
            async with self.pool.acquire() as connection:  # type: ignore
                if with_age and graph_name:
                    await self.configure_age(connection, graph_name)  # type: ignore
                elif with_age and not graph_name:
                    raise ValueError("Graph name is required when with_age is True")

                if data is None:
                    await connection.execute(sql)  # type: ignore
                else:
                    await connection.execute(sql, *data.values())  # type: ignore
        except (
            asyncpg.exceptions.UniqueViolationError,
            asyncpg.exceptions.DuplicateTableError,
        ) as e:
            if upsert:
                print("Key value duplicate, but upsert succeeded.")
            else:
                logger.error(f"Upsert error: {e}")
        except Exception as e:
            logger.error(f"PostgreSQL database,\nsql:{sql},\ndata:{data},\nerror:{e}")
            raise


class ClientManager:
    _instances: dict[str, Any] = {"db": None, "ref_count": 0}
    _lock = asyncio.Lock()

    @staticmethod
    def get_config() -> dict[str, Any]:
        config = configparser.ConfigParser()
        config.read("config.ini", "utf-8")

        return {
            "host": os.environ.get(
                "POSTGRES_HOST",
                config.get("postgres", "host", fallback="localhost"),
            ),
            "port": os.environ.get(
                "POSTGRES_PORT", config.get("postgres", "port", fallback=5432)
            ),
            "user": os.environ.get(
                "POSTGRES_USER", config.get("postgres", "user", fallback=None)
            ),
            "password": os.environ.get(
                "POSTGRES_PASSWORD",
                config.get("postgres", "password", fallback=None),
            ),
            "database": os.environ.get(
                "POSTGRES_DATABASE",
                config.get("postgres", "database", fallback=None),
            ),
            "workspace": os.environ.get(
                "POSTGRES_WORKSPACE",
                config.get("postgres", "workspace", fallback="default"),
            ),
            "max_connections": os.environ.get(
                "POSTGRES_MAX_CONNECTIONS",
                config.get("postgres", "max_connections", fallback=12),
            ),
        }

    @classmethod
    async def get_client(cls) -> PostgreSQLDB:
        async with cls._lock:
            if cls._instances["db"] is None:
                config = ClientManager.get_config()
                db = PostgreSQLDB(config)
                await db.initdb()
                await db.check_tables()
                cls._instances["db"] = db
                cls._instances["ref_count"] = 0
            cls._instances["ref_count"] += 1
            return cls._instances["db"]

    @classmethod
    async def release_client(cls, db: PostgreSQLDB):
        async with cls._lock:
            if db is not None:
                if db is cls._instances["db"]:
                    cls._instances["ref_count"] -= 1
                    if cls._instances["ref_count"] == 0:
                        await db.pool.close()
                        logger.info("Closed PostgreSQL database connection pool")
                        cls._instances["db"] = None
                else:
                    await db.pool.close()


@final
@dataclass
class PGKVStorage(BaseKVStorage):
    db: PostgreSQLDB = field(default=None)

    def __post_init__(self):
        self._max_batch_size = self.global_config["embedding_batch_num"]

    async def initialize(self):
        if self.db is None:
            self.db = await ClientManager.get_client()

    async def finalize(self):
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None

    ################ QUERY METHODS ################
    async def get_all(self) -> dict[str, Any]:
        """Get all data from storage

        Returns:
            Dictionary containing all stored data
        """
        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for get_all: {self.namespace}")
            return {}

        sql = f"SELECT * FROM {table_name} WHERE workspace=$1"
        params = {"workspace": self.db.workspace}

        try:
            results = await self.db.query(sql, params, multirows=True)

            if is_namespace(self.namespace, NameSpace.KV_STORE_LLM_RESPONSE_CACHE):
                result_dict = {}
                for row in results:
                    mode = row["mode"]
                    if mode not in result_dict:
                        result_dict[mode] = {}
                    result_dict[mode][row["id"]] = row
                return result_dict
            else:
                return {row["id"]: row for row in results}
        except Exception as e:
            logger.error(f"Error retrieving all data from {self.namespace}: {e}")
            return {}

    async def get_by_id(self, id: str) -> dict[str, Any] | None:
        """Get doc_full data by id."""
        sql = SQL_TEMPLATES["get_by_id_" + self.namespace]
        params = {"workspace": self.db.workspace, "id": id}
        if is_namespace(self.namespace, NameSpace.KV_STORE_LLM_RESPONSE_CACHE):
            array_res = await self.db.query(sql, params, multirows=True)
            res = {}
            for row in array_res:
                res[row["id"]] = row
            return res if res else None
        else:
            response = await self.db.query(sql, params)
            return response if response else None

    async def get_by_mode_and_id(self, mode: str, id: str) -> Union[dict, None]:
        """Specifically for llm_response_cache."""
        sql = SQL_TEMPLATES["get_by_mode_id_" + self.namespace]
        params = {"workspace": self.db.workspace, "mode": mode, "id": id}
        if is_namespace(self.namespace, NameSpace.KV_STORE_LLM_RESPONSE_CACHE):
            array_res = await self.db.query(sql, params, multirows=True)
            res = {}
            for row in array_res:
                res[row["id"]] = row
            return res
        else:
            return None

    # Query by id
    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Get doc_chunks data by id"""
        sql = SQL_TEMPLATES["get_by_ids_" + self.namespace].format(
            ids=",".join([f"'{id}'" for id in ids])
        )
        params = {"workspace": self.db.workspace}
        if is_namespace(self.namespace, NameSpace.KV_STORE_LLM_RESPONSE_CACHE):
            array_res = await self.db.query(sql, params, multirows=True)
            modes = set()
            dict_res: dict[str, dict] = {}
            for row in array_res:
                modes.add(row["mode"])
            for mode in modes:
                if mode not in dict_res:
                    dict_res[mode] = {}
            for row in array_res:
                dict_res[row["mode"]][row["id"]] = row
            return [{k: v} for k, v in dict_res.items()]
        else:
            return await self.db.query(sql, params, multirows=True)

    async def get_by_status(self, status: str) -> Union[list[dict[str, Any]], None]:
        """Specifically for llm_response_cache."""
        SQL = SQL_TEMPLATES["get_by_status_" + self.namespace]
        params = {"workspace": self.db.workspace, "status": status}
        return await self.db.query(SQL, params, multirows=True)

    async def filter_keys(self, keys: set[str]) -> set[str]:
        """Filter out duplicated content"""
        sql = SQL_TEMPLATES["filter_keys"].format(
            table_name=namespace_to_table_name(self.namespace),
            ids=",".join([f"'{id}'" for id in keys]),
        )
        params = {"workspace": self.db.workspace}
        try:
            res = await self.db.query(sql, params, multirows=True)
            if res:
                exist_keys = [key["id"] for key in res]
            else:
                exist_keys = []
            new_keys = set([s for s in keys if s not in exist_keys])
            return new_keys
        except Exception as e:
            logger.error(
                f"PostgreSQL database,\nsql:{sql},\nparams:{params},\nerror:{e}"
            )
            raise

    ################ INSERT METHODS ################
    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        logger.debug(f"Inserting {len(data)} to {self.namespace}")
        if not data:
            return

        if is_namespace(self.namespace, NameSpace.KV_STORE_TEXT_CHUNKS):
            pass
        elif is_namespace(self.namespace, NameSpace.KV_STORE_FULL_DOCS):
            for k, v in data.items():
                upsert_sql = SQL_TEMPLATES["upsert_doc_full"]
                _data = {
                    "id": k,
                    "content": v["content"],
                    "workspace": self.db.workspace,
                }
                await self.db.execute(upsert_sql, _data)
        elif is_namespace(self.namespace, NameSpace.KV_STORE_LLM_RESPONSE_CACHE):
            for mode, items in data.items():
                for k, v in items.items():
                    upsert_sql = SQL_TEMPLATES["upsert_llm_response_cache"]
                    _data = {
                        "workspace": self.db.workspace,
                        "id": k,
                        "original_prompt": v["original_prompt"],
                        "return_value": v["return"],
                        "mode": mode,
                    }

                    await self.db.execute(upsert_sql, _data)

    async def index_done_callback(self) -> None:
        # PG handles persistence automatically
        pass

    async def delete(self, ids: list[str]) -> None:
        """Delete specific records from storage by their IDs

        Args:
            ids (list[str]): List of document IDs to be deleted from storage

        Returns:
            None
        """
        if not ids:
            return

        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for deletion: {self.namespace}")
            return

        delete_sql = f"DELETE FROM {table_name} WHERE workspace=$1 AND id = ANY($2)"

        try:
            await self.db.execute(
                delete_sql, {"workspace": self.db.workspace, "ids": ids}
            )
            logger.debug(
                f"Successfully deleted {len(ids)} records from {self.namespace}"
            )
        except Exception as e:
            logger.error(f"Error while deleting records from {self.namespace}: {e}")

    async def drop_cache_by_modes(self, modes: list[str] | None = None) -> bool:
        """Delete specific records from storage by cache mode

        Args:
            modes (list[str]): List of cache modes to be dropped from storage

        Returns:
            bool: True if successful, False otherwise
        """
        if not modes:
            return False

        try:
            table_name = namespace_to_table_name(self.namespace)
            if not table_name:
                return False

            if table_name != "LIGHTRAG_LLM_CACHE":
                return False

            sql = f"""
            DELETE FROM {table_name}
            WHERE workspace = $1 AND mode = ANY($2)
            """
            params = {"workspace": self.db.workspace, "modes": modes}

            logger.info(f"Deleting cache by modes: {modes}")
            await self.db.execute(sql, params)
            return True
        except Exception as e:
            logger.error(f"Error deleting cache by modes {modes}: {e}")
            return False

    async def drop(self) -> dict[str, str]:
        """Drop the storage"""
        try:
            table_name = namespace_to_table_name(self.namespace)
            if not table_name:
                return {
                    "status": "error",
                    "message": f"Unknown namespace: {self.namespace}",
                }

            drop_sql = SQL_TEMPLATES["drop_specifiy_table_workspace"].format(
                table_name=table_name
            )
            await self.db.execute(drop_sql, {"workspace": self.db.workspace})
            return {"status": "success", "message": "data dropped"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


@final
@dataclass
class PGVectorStorage(BaseVectorStorage):
    db: PostgreSQLDB | None = field(default=None)

    def __post_init__(self):
        self._max_batch_size = self.global_config["embedding_batch_num"]
        config = self.global_config.get("vector_db_storage_cls_kwargs", {})
        cosine_threshold = config.get("cosine_better_than_threshold")
        if cosine_threshold is None:
            raise ValueError(
                "cosine_better_than_threshold must be specified in vector_db_storage_cls_kwargs"
            )
        self.cosine_better_than_threshold = cosine_threshold

    async def initialize(self):
        if self.db is None:
            self.db = await ClientManager.get_client()

    async def finalize(self):
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None

    def _upsert_chunks(
        self, item: dict[str, Any], current_time: datetime.datetime
    ) -> tuple[str, dict[str, Any]]:
        try:
            upsert_sql = SQL_TEMPLATES["upsert_chunk"]
            data: dict[str, Any] = {
                "workspace": self.db.workspace,
                "id": item["__id__"],
                "tokens": item["tokens"],
                "chunk_order_index": item["chunk_order_index"],
                "full_doc_id": item["full_doc_id"],
                "content": item["content"],
                "content_vector": json.dumps(item["__vector__"].tolist()),
                "file_path": item["file_path"],
                "create_time": current_time,
                "update_time": current_time,
            }
        except Exception as e:
            logger.error(f"Error to prepare upsert,\nsql: {e}\nitem: {item}")
            raise

        return upsert_sql, data

    def _upsert_entities(
        self, item: dict[str, Any], current_time: datetime.datetime
    ) -> tuple[str, dict[str, Any]]:
        upsert_sql = SQL_TEMPLATES["upsert_entity"]
        source_id = item["source_id"]
        if isinstance(source_id, str) and "<SEP>" in source_id:
            chunk_ids = source_id.split("<SEP>")
        else:
            chunk_ids = [source_id]

        data: dict[str, Any] = {
            "workspace": self.db.workspace,
            "id": item["__id__"],
            "entity_name": item["entity_name"],
            "content": item["content"],
            "content_vector": json.dumps(item["__vector__"].tolist()),
            "chunk_ids": chunk_ids,
            "file_path": item.get("file_path", None),
            "create_time": current_time,
            "update_time": current_time,
        }
        return upsert_sql, data

    def _upsert_relationships(
        self, item: dict[str, Any], current_time: datetime.datetime
    ) -> tuple[str, dict[str, Any]]:
        upsert_sql = SQL_TEMPLATES["upsert_relationship"]
        source_id = item["source_id"]
        if isinstance(source_id, str) and "<SEP>" in source_id:
            chunk_ids = source_id.split("<SEP>")
        else:
            chunk_ids = [source_id]

        data: dict[str, Any] = {
            "workspace": self.db.workspace,
            "id": item["__id__"],
            "source_id": item["src_id"],
            "target_id": item["tgt_id"],
            "content": item["content"],
            "content_vector": json.dumps(item["__vector__"].tolist()),
            "chunk_ids": chunk_ids,
            "file_path": item.get("file_path", None),
            "create_time": current_time,
            "update_time": current_time,
        }
        return upsert_sql, data

    async def upsert(self, data: dict[str, dict[str, Any]], build_vector_index: bool = True) -> None:
        logger.debug(f"Inserting {len(data)} to {self.namespace} with build_vector_index={build_vector_index}")
        if not data:
            return

        current_time = datetime.datetime.now(timezone.utc)
        list_data = [
            {
                "__id__": k,
                **{k1: v1 for k1, v1 in v.items()},
            }
            for k, v in data.items()
        ]

        if build_vector_index:
            contents = [v["content"] for v in data.values()]
            if contents:
                batches = [
                    contents[i : i + self._max_batch_size]
                    for i in range(0, len(contents), self._max_batch_size)
                ]
                embedding_tasks = [self.embedding_func(batch) for batch in batches]
                try:
                    results = await asyncio.gather(*embedding_tasks, return_exceptions=True)
                except Exception as e:
                    logger.error(f"An error occurred during embedding generation: {e}", exc_info=True)
                    raise
                # 检查并处理异常
                embeddings_list = []
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        logger.error(f"Error processing batch {i}: {str(result)}", exc_info=True)
                        raise result  # 或者根据需要处理异常
                    embeddings_list.append(result)
                embeddings = np.concatenate(embeddings_list)
                for i, d in enumerate(list_data):
                    d["__vector__"] = embeddings[i]

        for item in list_data:
            has_vector = "__vector__" in item

            if is_namespace(self.namespace, NameSpace.VECTOR_STORE_CHUNKS):
                table_name = "LIGHTRAG_DOC_CHUNKS"
                base_data = {
                    "workspace": self.db.workspace,
                    "id": item["__id__"],
                    "tokens": item["tokens"],
                    "chunk_order_index": item["chunk_order_index"],
                    "full_doc_id": item["full_doc_id"],
                    "content": item["content"],
                    "file_path": item["file_path"],
                    "create_time": current_time,
                    "update_time": current_time,
                }
                update_cols = ["tokens", "chunk_order_index", "full_doc_id", "content", "file_path", "update_time"]
                if has_vector:
                    base_data["content_vector"] = json.dumps(item["__vector__"].tolist())
                    update_cols.append("content_vector")

            elif is_namespace(self.namespace, NameSpace.VECTOR_STORE_ENTITIES):
                table_name = "LIGHTRAG_VDB_ENTITY"
                source_id = item["source_id"]
                chunk_ids = source_id.split("<SEP>") if isinstance(source_id, str) and "<SEP>" in source_id else [source_id]
                base_data = {
                    "workspace": self.db.workspace,
                    "id": item["__id__"],
                    "entity_name": item["entity_name"],
                    "content": item["content"],
                    "chunk_ids": chunk_ids,
                    "file_path": item.get("file_path", None),
                    "create_time": current_time,
                    "update_time": current_time,
                }
                update_cols = ["entity_name", "content", "chunk_ids", "file_path", "update_time"]
                if has_vector:
                    base_data["content_vector"] = json.dumps(item["__vector__"].tolist())
                    update_cols.append("content_vector")

            elif is_namespace(self.namespace, NameSpace.VECTOR_STORE_RELATIONSHIPS):
                table_name = "LIGHTRAG_VDB_RELATION"
                source_id = item["source_id"]
                chunk_ids = source_id.split("<SEP>") if isinstance(source_id, str) and "<SEP>" in source_id else [source_id]
                base_data = {
                    "workspace": self.db.workspace,
                    "id": item["__id__"],
                    "source_id": item["src_id"],
                    "target_id": item["tgt_id"],
                    "content": item["content"],
                    "chunk_ids": chunk_ids,
                    "file_path": item.get("file_path", None),
                    "create_time": current_time,
                    "update_time": current_time,
                }
                update_cols = ["source_id", "target_id", "content", "chunk_ids", "file_path", "update_time"]
                if has_vector:
                    base_data["content_vector"] = json.dumps(item["__vector__"].tolist())
                    update_cols.append("content_vector")
            else:
                raise ValueError(f"{self.namespace} is not supported")

            cols = list(base_data.keys())
            placeholders = ", ".join([f"${i+1}" for i in range(len(cols))])
            update_placeholders = ", ".join([f"{col} = EXCLUDED.{col}" for col in update_cols])
            
            upsert_sql = f'''
INSERT INTO {table_name} ({", ".join(cols)})
VALUES ({placeholders})
ON CONFLICT (id, workspace) DO UPDATE SET
{update_placeholders}
'''
            await self.db.execute(upsert_sql, base_data)

    #################### query method ###############
    async def query(
        self, query: str, top_k: int, ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        embeddings = await self.embedding_func(
            [query], _priority=5
        )  # higher priority for query
        embedding = embeddings[0]
        embedding_string = ",".join(map(str, embedding))
        # Use parameterized document IDs (None means search across all documents)
        sql = SQL_TEMPLATES[self.namespace].format(embedding_string=embedding_string)
        params = {
            "workspace": self.db.workspace,
            "doc_ids": ids,
            "better_than_threshold": self.cosine_better_than_threshold,
            "top_k": top_k,
        }
        results = await self.db.query(sql, params=params, multirows=True)
        return results

    async def index_done_callback(self) -> None:
        # PG handles persistence automatically
        pass

    async def delete(self, ids: list[str]) -> None:
        """Delete vectors with specified IDs from the storage.

        Args:
            ids: List of vector IDs to be deleted
        """
        if not ids:
            return

        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for vector deletion: {self.namespace}")
            return

        delete_sql = f"DELETE FROM {table_name} WHERE workspace=$1 AND id = ANY($2)"

        try:
            await self.db.execute(
                delete_sql, {"workspace": self.db.workspace, "ids": ids}
            )
            logger.debug(
                f"Successfully deleted {len(ids)} vectors from {self.namespace}"
            )
        except Exception as e:
            logger.error(f"Error while deleting vectors from {self.namespace}: {e}")

    async def delete_by_doc_id(self, doc_id:str, delete_chunk_ids: list[str]) -> None:
        """Delete specific records from storage by their IDs

        Args:
            ids (list[str]): List of document IDs to be deleted from storage

        Returns:
            None
        """
        if not delete_chunk_ids or len(delete_chunk_ids) == 0:
            logger.info(f"No chunk ids to delete in postgresql vector db")
            return

        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for deletion: {self.namespace}")
            return

        # 基于uuid和chunk_ids检索匹配的数据记录，实现分页的查询
        # file_path字段存储了以<SEP>为分隔符的文件路径信息。

        # section1:  基于chunk ids和file path检索匹配的lightrag_vdb_relation记录，分页方式；检索条件为： chunk_ids包含任何入口list的chunk_id, file_path中包含该uuid
        # 针对每一条记录，针对其file_path进行<SEP>分割，判断其中包含uuid的元素，然后删除该元素；如果剩余的 元素个数大于1，则将剩余的元素拼接成新的file_path，否则将该记录标记为删除，放入待删除列表中
        # 对于file_path中剩余元素大于1的记录，则将chunk_ids中的数组元素，移除入口参数中的chunk_ids中的元素;如果chunk_ids 中没有元素，则将该记录标记为删除，放入待删除列表中；
        #  chunk_ids如果不为空，则记录其中chunk_ids中包含入口chunk_ids元素的位置；由于content的内容以<SEP>作为分隔符，与chunk_ids中的chunk_id一一对应，按照其中的位置，删除对应的content内容，然后更新content内容；
        # 记录上述待更新的记录列表，等待批量更新
        # 基于上述待更新的列表和content，重新计算其content_vector, 然后批量更新记录。
        start_time  = time.time()
        entity_to_update = {}
        entity_to_delete = {}
        current_page = 1
        page_size = 50
        while True:
            result = await self.paginated_query(table_name, current_page, page_size, where_clause=" where workspace=$1 AND file_path LIKE $2 AND chunk_ids && $3", \
                                                params={"workspace": self.db.workspace, "file_path": f"%{doc_id}%", "chunk_ids": delete_chunk_ids})
            logger.info(f"current page:{current_page} , page size:{page_size}, result size:{len(result)}")

            # process the data
            for row in result:
                if self.__is_vector_record_delete__(row, doc_id, delete_chunk_ids):
                    entity_to_delete[row["id"]] = row
                else:
                     entity_to_update[row["id"]] = self.__update_record__(row, doc_id, delete_chunk_ids)

            logger.info(f"entity_to_update:{len(entity_to_update)}, entity_to_delete:{len(entity_to_delete)}")
            # execute the modification operation
            await self.__update_entity_records__(entity_to_update)
            await self.__delete_entity_records__(entity_to_delete)

            if not result or len(result) < page_size:
                logger.info(f"Loop the data to the end, total page:{current_page}")
                break;
            current_page += 1
        logger.info(f"Finished the loop in table {table_name}, total time:{time.time() - start_time}")

    async def __update_entity_records__(self, entity_to_updates: dict[str, dict[str, Any]]) -> None:
        """
           更新记录
        Args:
            entity_to_updates: 

        Returns:

        """
        if not entity_to_updates or len(entity_to_updates) == 0:
            logger.info(f"No entity to update in postgresql vector db")
            return

        table_name = namespace_to_table_name(self.namespace)

        if not table_name:
            logger.error(f"Unknown namespace for deletion: {self.namespace}")
            return

        # update  the entity records with embedding vector
        id_list = list(entity_to_updates.keys())
        chunk_size = 20
        text_chunk_two_array = [id_list[i:i + chunk_size] for i in range(0, len(id_list), chunk_size)]

        embedding_tasks = [self.embedding_func(batch) for batch in text_chunk_two_array]
        embeddings_list = await asyncio.gather(*embedding_tasks)
        embeddings = np.concatenate(embeddings_list)

        for index, id in enumerate(id_list):
             vector_array = embeddings[index]
             entity_to_updates[id]["content_vector"] = json.dumps(vector_array.tolist())


        for id, row in entity_to_updates.items():
            update_sql = f"UPDATE {table_name} SET file_path=$1, content=$2, content_vector=$3 WHERE id=$4"
            await self.db.execute(update_sql, {"file_path":row["file_path"], "content":row["content"], "content_vector": row["content_vector"], "id": id})

        logger.info(f"Update entity records successfully: {len(entity_to_updates)}")


    async def  __delete_entity_records__(self, entity_to_delete: dict[str, dict[str, Any]]) -> None:
         """
          删除记录
         Args:
            self:
            entity_to_delete:

         Returns:

         """
         if not entity_to_delete or len(entity_to_delete) == 0:
             logger.info(f"No entity to delete in postgresql vector db")
             return

         table_name = namespace_to_table_name(self.namespace)
         if not table_name:
             logger.error(f"Unknown namespace for deletion: {self.namespace}")
             return

         # 提取所有需要删除的 id
         ids_to_delete = list(entity_to_delete.keys())

         # 构造批量删除 SQL 语句
         delete_sql = f"DELETE FROM {table_name} WHERE id = ANY($1)"

         try:
             await self.db.execute(delete_sql, {"ids": ids_to_delete})
             logger.info(f"Successfully deleted {len(ids_to_delete)} entity records")
         except Exception as e:
             logger.error(f"Error while deleting entity records: {e}")
         logger.info(f"Delete entity records successfully: {len(entity_to_delete)}")


    def __update_record__(self, row, uuid:str, delete_chunk_ids:list) -> dict:
        """
          更新记录
        """
        dict_data = dict(row)
        file_list = dict_data["file_path"].split(GRAPH_FIELD_SEP)
        filtered_file_list = []

        for file_path_str in file_list:
            if not uuid in file_path_str:
                filtered_file_list.append(file_path_str)
        if len(filtered_file_list) > 0:
            dict_data["file_path"] = GRAPH_FIELD_SEP.join(filtered_file_list)

        content_list = dict_data["content"].split(GRAPH_FIELD_SEP)
        removed_idx_list = []
        updated_chunk_ids = []
        for index, chunk_id in enumerate(dict_data['chunk_ids']):
            if  chunk_id in delete_chunk_ids:
                removed_idx_list.append(index)
            else:
                 updated_chunk_ids.append(chunk_id)

        new_list = [x for i, x in enumerate(content_list) if i not in removed_idx_list]
        dict_data["content"] = GRAPH_FIELD_SEP.join(new_list)
        dict_data["chunk_ids"] = updated_chunk_ids

        return dict_data

    def __is_vector_record_delete__(self, row, uuid:str, delete_chunk_ids:list) -> bool:
        """
          判断当前记录是否需要删除
         判断标准： 文件中有不包含uuid的文件路径，则认为更新/False。否则为True
        Args:
            row:

        Returns: bool
        """
        if row["file_path"] is None:
            return True

        file_list = row["file_path"].split(GRAPH_FIELD_SEP)

        for idx, file_path_str in enumerate(file_list):
            if file_path_str is None:
                logger.warning(f"file_list[{idx}] is None, skipping check.")
                continue
            if uuid not in file_path_str:
                return False

        return True

    async def paginated_query(self, table_name:str, page:int=1, page_size:int=20, where_clause:str="", params:dict=None):
        offset  = (page - 1) * page_size
        sql_str =  f"SELECT * FROM {table_name}"

        if where_clause:
            sql_str += f" {where_clause}"

        sql_str += f" ORDER BY id LIMIT {page_size} OFFSET {offset}"
        return await  self.db.query(sql_str, params=params, multirows=True)

    async def delete_entity(self, entity_name: str) -> None:
        """Delete an entity by its name from the vector storage.

        delete_by_chunkidsArgs:
            entity_name: The name of the entity to delete
        """
        try:
            # Construct SQL to delete the entity
            delete_sql = """DELETE FROM LIGHTRAG_VDB_ENTITY
                            WHERE workspace=$1 AND entity_name=$2"""

            await self.db.execute(
                delete_sql, {"workspace": self.db.workspace, "entity_name": entity_name}
            )
            logger.debug(f"Successfully deleted entity {entity_name}")
        except Exception as e:
            logger.error(f"Error deleting entity {entity_name}: {e}")

    async def delete_entity_relation(self, entity_name: str) -> None:
        """Delete all relations associated with an entity.

        Args:
            entity_name: The name of the entity whose relations should be deleted
        """
        try:
            # Delete relations where the entity is either the source or target
            delete_sql = """DELETE FROM LIGHTRAG_VDB_RELATION
                            WHERE workspace=$1 AND (source_id=$2 OR target_id=$2)"""

            await self.db.execute(
                delete_sql, {"workspace": self.db.workspace, "entity_name": entity_name}
            )
            logger.debug(f"Successfully deleted relations for entity {entity_name}")
        except Exception as e:
            logger.error(f"Error deleting relations for entity {entity_name}: {e}")

    async def get_by_id(self, id: str) -> dict[str, Any] | None:
        """Get vector data by its ID

        Args:
            id: The unique identifier of the vector

        Returns:
            The vector data if found, or None if not found
        """
        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for ID lookup: {self.namespace}")
            return None

        query = f"SELECT *, EXTRACT(EPOCH FROM create_time)::BIGINT as created_at FROM {table_name} WHERE workspace=$1 AND id=$2"
        params = {"workspace": self.db.workspace, "id": id}

        try:
            result = await self.db.query(query, params)
            if result:
                return dict(result)
            return None
        except Exception as e:
            logger.error(f"Error retrieving vector data for ID {id}: {e}")
            return None

    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Get multiple vector data by their IDs

        Args:
            ids: List of unique identifiers

        Returns:
            List of vector data objects that were found
        """
        if not ids:
            return []

        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for IDs lookup: {self.namespace}")
            return []

        ids_str = ",".join([f"'{id}'" for id in ids])
        query = f"SELECT *, EXTRACT(EPOCH FROM create_time)::BIGINT as created_at FROM {table_name} WHERE workspace=$1 AND id IN ({ids_str})"
        params = {"workspace": self.db.workspace}

        try:
            results = await self.db.query(query, params, multirows=True)
            return [dict(record) for record in results]
        except Exception as e:
            logger.error(f"Error retrieving vector data for IDs {ids}: {e}")
            return []

    async def drop(self) -> dict[str, str]:
        """Drop the storage"""
        try:
            table_name = namespace_to_table_name(self.namespace)
            if not table_name:
                return {
                    "status": "error",
                    "message": f"Unknown namespace: {self.namespace}",
                }

            drop_sql = SQL_TEMPLATES["drop_specifiy_table_workspace"].format(
                table_name=table_name
            )
            await self.db.execute(drop_sql, {"workspace": self.db.workspace})
            return {"status": "success", "message": "data dropped"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


@final
@dataclass
class PGDocStatusStorage(DocStatusStorage):
    db: PostgreSQLDB = field(default=None)

    async def initialize(self):
        if self.db is None:
            self.db = await ClientManager.get_client()

    async def finalize(self):
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None

    async def filter_keys(self, keys: set[str]) -> set[str]:
        """Filter out duplicated content"""
        sql = SQL_TEMPLATES["filter_keys"].format(
            table_name=namespace_to_table_name(self.namespace),
            ids=",".join([f"'{id}'" for id in keys]),
        )
        params = {"workspace": self.db.workspace}
        try:
            res = await self.db.query(sql, params, multirows=True)
            if res:
                exist_keys = [key["id"] for key in res]
            else:
                exist_keys = []
            new_keys = set([s for s in keys if s not in exist_keys])
            print(f"keys: {keys}")
            print(f"new_keys: {new_keys}")
            return new_keys
        except Exception as e:
            logger.error(
                f"PostgreSQL database,\nsql:{sql},\nparams:{params},\nerror:{e}"
            )
            raise

    async def get_by_id(self, id: str) -> Union[dict[str, Any], None]:
        sql = "select * from LIGHTRAG_DOC_STATUS where workspace=$1 and id=$2"
        params = {"workspace": self.db.workspace, "id": id}
        result = await self.db.query(sql, params, True)
        if result is None or result == []:
            return None
        else:
            return dict(
                content=result[0]["content"],
                content_length=result[0]["content_length"],
                content_summary=result[0]["content_summary"],
                status=result[0]["status"],
                chunks_count=result[0]["chunks_count"],
                created_at=result[0]["created_at"],
                updated_at=result[0]["updated_at"],
                file_path=result[0]["file_path"],
            )

    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Get doc_chunks data by multiple IDs."""
        if not ids:
            return []

        sql = "SELECT * FROM LIGHTRAG_DOC_STATUS WHERE workspace=$1 AND id = ANY($2)"
        params = {"workspace": self.db.workspace, "ids": ids}

        results = await self.db.query(sql, params, True)

        if not results:
            return []
        return [
            {
                "content": row["content"],
                "content_length": row["content_length"],
                "content_summary": row["content_summary"],
                "status": row["status"],
                "chunks_count": row["chunks_count"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "file_path": row["file_path"],
            }
            for row in results
        ]

    async def get_status_counts(self) -> dict[str, int]:
        """Get counts of documents in each status"""
        sql = """SELECT status as "status", COUNT(1) as "count"
                   FROM LIGHTRAG_DOC_STATUS
                  where workspace=$1 GROUP BY STATUS
                 """
        result = await self.db.query(sql, {"workspace": self.db.workspace}, True)
        counts = {}
        for doc in result:
            counts[doc["status"]] = doc["count"]
        return counts

    async def get_docs_by_status(
        self, status: DocStatus, limit: int | None = None
    ) -> dict[str, DocProcessingStatus]:
        """all documents with a specific status"""
        sql = "select * from LIGHTRAG_DOC_STATUS where workspace=$1 and status=$2"
        params = {"workspace": self.db.workspace, "status": status.value}
        if limit is not None:
            sql += f" LIMIT ${len(params) + 1}"
            params["limit"] = limit

        result = await self.db.query(sql, params, True)
        docs_by_status = {
            element["id"]: DocProcessingStatus(
                content=element["content"],
                content_summary=element["content_summary"],
                content_length=element["content_length"],
                status=element["status"],
                created_at=element["created_at"],
                updated_at=element["updated_at"],
                chunks_count=element["chunks_count"],
                file_path=element["file_path"],
            )
            for element in result
        }
        return docs_by_status

    async def index_done_callback(self) -> None:
        # PG handles persistence automatically
        pass

    async def delete(self, ids: list[str]) -> None:
        """Delete specific records from storage by their IDs

        Args:
            ids (list[str]): List of document IDs to be deleted from storage

        Returns:
            None
        """
        if not ids:
            return

        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for deletion: {self.namespace}")
            return

        delete_sql = f"DELETE FROM {table_name} WHERE workspace=$1 AND id = ANY($2)"

        try:
            await self.db.execute(
                delete_sql, {"workspace": self.db.workspace, "ids": ids}
            )
            logger.debug(
                f"Successfully deleted {len(ids)} records from {self.namespace}"
            )
        except Exception as e:
            logger.error(f"Error while deleting records from {self.namespace}: {e}")

    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        """Update or insert document status

        Args:
            data: dictionary of document IDs and their status data
        """
        logger.debug(f"Inserting {len(data)} to {self.namespace}")
        if not data:
            return

        def parse_datetime(dt_str):
            if dt_str is None:
                return None
            if isinstance(dt_str, (datetime.date, datetime.datetime)):
                # If it's a datetime object without timezone info, remove timezone info
                if isinstance(dt_str, datetime.datetime):
                    # Remove timezone info, return naive datetime object
                    return dt_str.replace(tzinfo=None)
                return dt_str
            try:
                # Process ISO format string with timezone
                dt = datetime.datetime.fromisoformat(dt_str)
                # Remove timezone info, return naive datetime object
                return dt.replace(tzinfo=None)
            except (ValueError, TypeError):
                logger.warning(f"Unable to parse datetime string: {dt_str}")
                return None

        # Modified SQL to include created_at and updated_at in both INSERT and UPDATE operations
        # Both fields are updated from the input data in both INSERT and UPDATE cases
        sql = """insert into LIGHTRAG_DOC_STATUS(workspace,id,content,content_summary,content_length,chunks_count,status,file_path,created_at,updated_at)
                 values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                  on conflict(id,workspace) do update set
                  content = EXCLUDED.content,
                  content_summary = EXCLUDED.content_summary,
                  content_length = EXCLUDED.content_length,
                  chunks_count = EXCLUDED.chunks_count,
                  status = EXCLUDED.status,
                  file_path = EXCLUDED.file_path,
                  created_at = EXCLUDED.created_at,
                  updated_at = EXCLUDED.updated_at"""
        for k, v in data.items():
            # Remove timezone information, store utc time in db
            created_at = parse_datetime(v.get("created_at"))
            updated_at = parse_datetime(v.get("updated_at"))

            # chunks_count is optional
            await self.db.execute(
                sql,
                {
                    "workspace": self.db.workspace,
                    "id": k,
                    "content": v["content"],
                    "content_summary": v["content_summary"],
                    "content_length": v["content_length"],
                    "chunks_count": v["chunks_count"] if "chunks_count" in v else -1,
                    "status": v["status"],
                    "file_path": v["file_path"],
                    "created_at": created_at,  # Use the converted datetime object
                    "updated_at": updated_at,  # Use the converted datetime object
                },
            )

    async def drop(self) -> dict[str, str]:
        """Drop the storage"""
        try:
            table_name = namespace_to_table_name(self.namespace)
            if not table_name:
                return {
                    "status": "error",
                    "message": f"Unknown namespace: {self.namespace}",
                }

            drop_sql = SQL_TEMPLATES["drop_specifiy_table_workspace"].format(
                table_name=table_name
            )
            await self.db.execute(drop_sql, {"workspace": self.db.workspace})
            return {"status": "success", "message": "data dropped"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


class PGGraphQueryException(Exception):
    """Exception for the AGE queries."""

    def __init__(self, exception: Union[str, dict[str, Any]]) -> None:
        if isinstance(exception, dict):
            self.message = exception["message"] if "message" in exception else "unknown"
            self.details = exception["details"] if "details" in exception else "unknown"
        else:
            self.message = exception
            self.details = "unknown"

    def get_message(self) -> str:
        return self.message

    def get_details(self) -> Any:
        return self.details


@final
@dataclass
class PGGraphStorage(BaseGraphStorage):
    def __post_init__(self):
        self.graph_name = self.namespace or os.environ.get("AGE_GRAPH_NAME", "lightrag")
        self.db: PostgreSQLDB | None = None

    @staticmethod
    def _normalize_node_id(node_id: str) -> str:
        """
        Normalize node ID to ensure special characters are properly handled in Cypher queries.

        Args:
            node_id: The original node ID

        Returns:
            Normalized node ID suitable for Cypher queries
        """
        # Escape backslashes
        normalized_id = node_id
        normalized_id = normalized_id.replace("\\", "\\\\")
        normalized_id = normalized_id.replace('"', '\\"')
        return normalized_id

    async def initialize(self):
        if self.db is None:
            self.db = await ClientManager.get_client()

        # Execute each statement separately and ignore errors
        queries = [
            f"SELECT create_graph('{self.graph_name}')",
            f"SELECT create_vlabel('{self.graph_name}', 'base');",
            f"SELECT create_elabel('{self.graph_name}', 'DIRECTED');",
            # f'CREATE INDEX CONCURRENTLY vertex_p_idx ON {self.graph_name}."_ag_label_vertex" (id)',
            f'CREATE INDEX CONCURRENTLY vertex_idx_node_id ON {self.graph_name}."_ag_label_vertex" (ag_catalog.agtype_access_operator(properties, \'"entity_id"\'::agtype))',
            # f'CREATE INDEX CONCURRENTLY edge_p_idx ON {self.graph_name}."_ag_label_edge" (id)',
            f'CREATE INDEX CONCURRENTLY edge_sid_idx ON {self.graph_name}."_ag_label_edge" (start_id)',
            f'CREATE INDEX CONCURRENTLY edge_eid_idx ON {self.graph_name}."_ag_label_edge" (end_id)',
            f'CREATE INDEX CONCURRENTLY edge_seid_idx ON {self.graph_name}."_ag_label_edge" (start_id,end_id)',
            f'CREATE INDEX CONCURRENTLY directed_p_idx ON {self.graph_name}."DIRECTED" (id)',
            f'CREATE INDEX CONCURRENTLY directed_eid_idx ON {self.graph_name}."DIRECTED" (end_id)',
            f'CREATE INDEX CONCURRENTLY directed_sid_idx ON {self.graph_name}."DIRECTED" (start_id)',
            f'CREATE INDEX CONCURRENTLY directed_seid_idx ON {self.graph_name}."DIRECTED" (start_id,end_id)',
            f'CREATE INDEX CONCURRENTLY entity_p_idx ON {self.graph_name}."base" (id)',
            f'CREATE INDEX CONCURRENTLY entity_idx_node_id ON {self.graph_name}."base" (ag_catalog.agtype_access_operator(properties, \'"entity_id"\'::agtype))',
            f'CREATE INDEX CONCURRENTLY entity_node_id_gin_idx ON {self.graph_name}."base" using gin(properties)',
            f'ALTER TABLE {self.graph_name}."DIRECTED" CLUSTER ON directed_sid_idx',
        ]

        for query in queries:
            try:
                await self.db.execute(
                    query,
                    upsert=True,
                    with_age=True,
                    graph_name=self.graph_name,
                )
                # logger.info(f"Successfully executed: {query}")
            except Exception:
                continue

    async def finalize(self):
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None

    async def index_done_callback(self) -> None:
        # PG handles persistence automatically
        pass

    @staticmethod
    def _record_to_dict(record: asyncpg.Record) -> dict[str, Any]:
        """
        Convert a record returned from an age query to a dictionary

        Args:
            record (): a record from an age query result

        Returns:
            dict[str, Any]: a dictionary representation of the record where
                the dictionary key is the field name and the value is the
                value converted to a python type
        """
        # result holder
        d = {}

        # prebuild a mapping of vertex_id to vertex mappings to be used
        # later to build edges
        vertices = {}
        for k in record.keys():
            v = record[k]
            # agtype comes back '{key: value}::type' which must be parsed
            if isinstance(v, str) and "::" in v:
                if v.startswith("[") and v.endswith("]"):
                    if "::vertex" not in v:
                        continue
                    v = v.replace("::vertex", "")
                    vertexes = json.loads(v)
                    for vertex in vertexes:
                        vertices[vertex["id"]] = vertex.get("properties")
                else:
                    dtype = v.split("::")[-1]
                    v = v.split("::")[0]
                    if dtype == "vertex":
                        vertex = json.loads(v)
                        vertices[vertex["id"]] = vertex.get("properties")

        # iterate returned fields and parse appropriately
        for k in record.keys():
            v = record[k]
            if isinstance(v, str) and "::" in v:
                if v.startswith("[") and v.endswith("]"):
                    if "::vertex" in v:
                        v = v.replace("::vertex", "")
                        d[k] = json.loads(v)

                    elif "::edge" in v:
                        v = v.replace("::edge", "")
                        d[k] = json.loads(v)
                    else:
                        print("WARNING: unsupported type")
                        continue

                else:
                    dtype = v.split("::")[-1]
                    v = v.split("::")[0]
                    if dtype == "vertex":
                        d[k] = json.loads(v)
                    elif dtype == "edge":
                        d[k] = json.loads(v)
            else:
                d[k] = v  # Keep as string

        return d

    @staticmethod
    def _format_properties(
        properties: dict[str, Any], _id: Union[str, None] = None
    ) -> str:
        """
        Convert a dictionary of properties to a string representation that
        can be used in a cypher query insert/merge statement.

        Args:
            properties (dict[str,str]): a dictionary containing node/edge properties
            _id (Union[str, None]): the id of the node or None if none exists

        Returns:
            str: the properties dictionary as a properly formatted string
        """
        props = []
        # wrap property key in backticks to escape
        for k, v in properties.items():
            prop = f"`{k}`: {json.dumps(v)}"
            props.append(prop)
        if _id is not None and "id" not in properties:
            props.append(
                f"id: {json.dumps(_id)}" if isinstance(_id, str) else f"id: {_id}"
            )
        return "{" + ", ".join(props) + "}"

    async def _query(
        self,
        query: str,
        readonly: bool = True,
        upsert: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Query the graph by taking a cypher query, converting it to an
        age compatible query, executing it and converting the result

        Args:
            query (str): a cypher query to be executed

        Returns:
            list[dict[str, Any]]: a list of dictionaries containing the result set
        """
        try:
            if readonly:
                data = await self.db.query(
                    query,
                    multirows=True,
                    with_age=True,
                    graph_name=self.graph_name,
                )
            else:
                data = await self.db.execute(
                    query,
                    upsert=upsert,
                    with_age=True,
                    graph_name=self.graph_name,
                )

        except Exception as e:
            raise PGGraphQueryException(
                {
                    "message": f"Error executing graph query: {query}",
                    "wrapped": query,
                    "detail": str(e),
                }
            ) from e

        if data is None:
            result = []
        # decode records
        else:
            result = [self._record_to_dict(d) for d in data]

        return result

    async def has_node(self, node_id: str) -> bool:
        entity_name_label = self._normalize_node_id(node_id)

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (n:base {entity_id: "%s"})
                     RETURN count(n) > 0 AS node_exists
                   $$) AS (node_exists bool)""" % (self.graph_name, entity_name_label)

        single_result = (await self._query(query))[0]

        return single_result["node_exists"]

    async def has_edge(self, source_node_id: str, target_node_id: str) -> bool:
        src_label = self._normalize_node_id(source_node_id)
        tgt_label = self._normalize_node_id(target_node_id)

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (a:base {entity_id: "%s"})-[r]-(b:base {entity_id: "%s"})
                     RETURN COUNT(r) > 0 AS edge_exists
                   $$) AS (edge_exists bool)""" % (
            self.graph_name,
            src_label,
            tgt_label,
        )

        single_result = (await self._query(query))[0]

        return single_result["edge_exists"]

    async def get_node(self, node_id: str) -> dict[str, str] | None:
        """Get node by its label identifier, return only node properties"""

        label = self._normalize_node_id(node_id)
        query = """SELECT * FROM cypher('%s', $$
                     MATCH (n:base {entity_id: "%s"})
                     RETURN n
                   $$) AS (n agtype)""" % (self.graph_name, label)
        record = await self._query(query)
        if record:
            node = record[0]
            node_dict = node["n"]["properties"]

            # Process string result, parse it to JSON dictionary
            if isinstance(node_dict, str):
                try:
                    import json

                    node_dict = json.loads(node_dict)
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse node string: {node_dict}")

            return node_dict
        return None

    async def node_degree(self, node_id: str) -> int:
        label = self._normalize_node_id(node_id)

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (n:base {entity_id: "%s"})-[r]-()
                     RETURN count(r) AS total_edge_count
                   $$) AS (total_edge_count integer)""" % (self.graph_name, label)
        record = (await self._query(query))[0]
        if record:
            edge_count = int(record["total_edge_count"])
            return edge_count

    async def edge_degree(self, src_id: str, tgt_id: str) -> int:
        src_degree = await self.node_degree(src_id)
        trg_degree = await self.node_degree(tgt_id)

        # Convert None to 0 for addition
        src_degree = 0 if src_degree is None else src_degree
        trg_degree = 0 if trg_degree is None else trg_degree

        degrees = int(src_degree) + int(trg_degree)

        return degrees

    async def get_edge(
        self, source_node_id: str, target_node_id: str
    ) -> dict[str, str] | None:
        """Get edge properties between two nodes"""

        src_label = self._normalize_node_id(source_node_id)
        tgt_label = self._normalize_node_id(target_node_id)

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (a:base {entity_id: "%s"})-[r]-(b:base {entity_id: "%s"})
                     RETURN properties(r) as edge_properties
                     LIMIT 1
                   $$) AS (edge_properties agtype)""" % (
            self.graph_name,
            src_label,
            tgt_label,
        )
        record = await self._query(query)
        if record and record[0] and record[0]["edge_properties"]:
            result = record[0]["edge_properties"]

            # Process string result, parse it to JSON dictionary
            if isinstance(result, str):
                try:
                    import json

                    result = json.loads(result)
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse edge string: {result}")

            return result

    async def get_node_edges(self, source_node_id: str) -> list[tuple[str, str]] | None:
        """
        Retrieves all edges (relationships) for a particular node identified by its label.
        :return: list of dictionaries containing edge information
        """
        label = self._normalize_node_id(source_node_id)

        query = """SELECT * FROM cypher('%s', $$
                      MATCH (n:base {entity_id: "%s"})
                      OPTIONAL MATCH (n)-[]-(connected:base)
                      RETURN n.entity_id AS source_id, connected.entity_id AS connected_id
                    $$) AS (source_id text, connected_id text)""" % (
            self.graph_name,
            label,
        )

        results = await self._query(query)
        edges = []
        for record in results:
            source_id = record["source_id"]
            connected_id = record["connected_id"]

            if source_id and connected_id:
                edges.append((source_id, connected_id))

        return edges

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((PGGraphQueryException,)),
    )
    async def upsert_node(self, node_id: str, node_data: dict[str, str]) -> None:
        """
        Upsert a node in the Neo4j database.

        Args:
            node_id: The unique identifier for the node (used as label)
            node_data: Dictionary of node properties
        """
        if "entity_id" not in node_data:
            raise ValueError(
                "PostgreSQL: node properties must contain an 'entity_id' field"
            )

        label = self._normalize_node_id(node_id)
        properties = self._format_properties(node_data)

        query = """SELECT * FROM cypher('%s', $$
                     MERGE (n:base {entity_id: "%s"})
                     SET n += %s
                     RETURN n
                   $$) AS (n agtype)""" % (
            self.graph_name,
            label,
            properties,
        )

        try:
            await self._query(query, readonly=False, upsert=True)

        except Exception:
            logger.error(f"POSTGRES, upsert_node error on node_id: `{node_id}`")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((PGGraphQueryException,)),
    )
    async def upsert_edge(
        self, source_node_id: str, target_node_id: str, edge_data: dict[str, str]
    ) -> None:
        """
        Upsert an edge and its properties between two nodes identified by their labels.

        Args:
            source_node_id (str): Label of the source node (used as identifier)
            target_node_id (str): Label of the target node (used as identifier)
            edge_data (dict): dictionary of properties to set on the edge
        """
        src_label = self._normalize_node_id(source_node_id)
        tgt_label = self._normalize_node_id(target_node_id)
        edge_properties = self._format_properties(edge_data)

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (source:base {entity_id: "%s"})
                     WITH source
                     MATCH (target:base {entity_id: "%s"})
                     MERGE (source)-[r:DIRECTED]-(target)
                     SET r += %s
                     SET r += %s
                     RETURN r
                   $$) AS (r agtype)""" % (
            self.graph_name,
            src_label,
            tgt_label,
            edge_properties,
            edge_properties,  # https://github.com/HKUDS/LightRAG/issues/1438#issuecomment-2826000195
        )

        try:
            await self._query(query, readonly=False, upsert=True)

        except Exception:
            logger.error(
                f"POSTGRES, upsert_edge error on edge: `{source_node_id}`-`{target_node_id}`"
            )
            raise

    async def delete_node(self, node_id: str) -> None:
        """
        Delete a node from the graph.

        Args:
            node_id (str): The ID of the node to delete.
        """
        label = self._normalize_node_id(node_id)

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (n:base {entity_id: "%s"})
                     DETACH DELETE n
                   $$) AS (n agtype)""" % (self.graph_name, label)

        try:
            await self._query(query, readonly=False)
        except Exception as e:
            logger.error("Error during node deletion: {%s}", e)
            raise

    async def remove_nodes(self, node_ids: list[str]) -> None:
        """
        Remove multiple nodes from the graph.

        Args:
            node_ids (list[str]): A list of node IDs to remove.
        """
        node_ids = [self._normalize_node_id(node_id) for node_id in node_ids]
        node_id_list = ", ".join([f'"{node_id}"' for node_id in node_ids])

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (n:base)
                     WHERE n.entity_id IN [%s]
                     DETACH DELETE n
                   $$) AS (n agtype)""" % (self.graph_name, node_id_list)

        try:
            await self._query(query, readonly=False)
        except Exception as e:
            logger.error("Error during node removal: {%s}", e)
            raise

    async def remove_edges(self, edges: list[tuple[str, str]]) -> None:
        """
        Remove multiple edges from the graph.

        Args:
            edges (list[tuple[str, str]]): A list of edges to remove, where each edge is a tuple of (source_node_id, target_node_id).
        """
        for source, target in edges:
            src_label = self._normalize_node_id(source)
            tgt_label = self._normalize_node_id(target)

            query = """SELECT * FROM cypher('%s', $$
                         MATCH (a:base {entity_id: "%s"})-[r]-(b:base {entity_id: "%s"})
                         DELETE r
                       $$) AS (r agtype)""" % (self.graph_name, src_label, tgt_label)

            try:
                await self._query(query, readonly=False)
                logger.debug(f"Deleted edge from '{source}' to '{target}'")
            except Exception as e:
                logger.error(f"Error during edge deletion: {str(e)}")
                raise

    async def get_nodes_batch(self, node_ids: list[str]) -> dict[str, dict]:
        """
        Retrieve multiple nodes in one query using UNWIND.

        Args:
            node_ids: List of node entity IDs to fetch.

        Returns:
            A dictionary mapping each node_id to its node data (or None if not found).
        """
        if not node_ids:
            return {}

        # Format node IDs for the query
        formatted_ids = ", ".join(
            ['"' + self._normalize_node_id(node_id) + '"' for node_id in node_ids]
        )

        query = """SELECT * FROM cypher('%s', $$
                     UNWIND [%s] AS node_id
                     MATCH (n:base {entity_id: node_id})
                     RETURN node_id, n
                   $$) AS (node_id text, n agtype)""" % (self.graph_name, formatted_ids)

        results = await self._query(query)

        # Build result dictionary
        nodes_dict = {}
        for result in results:
            if result["node_id"] and result["n"]:
                node_dict = result["n"]["properties"]

                # Process string result, parse it to JSON dictionary
                if isinstance(node_dict, str):
                    try:
                        import json

                        node_dict = json.loads(node_dict)
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Failed to parse node string in batch: {node_dict}"
                        )

                # Remove the 'base' label if present in a 'labels' property
                if "labels" in node_dict:
                    node_dict["labels"] = [
                        label for label in node_dict["labels"] if label != "base"
                    ]
                nodes_dict[result["node_id"]] = node_dict

        return nodes_dict

    async def node_degrees_batch(self, node_ids: list[str]) -> dict[str, int]:
        """
        Retrieve the degree for multiple nodes in a single query using UNWIND.
        Calculates the total degree by counting distinct relationships.
        Uses separate queries for outgoing and incoming edges.

        Args:
            node_ids: List of node labels (entity_id values) to look up.

        Returns:
            A dictionary mapping each node_id to its degree (total number of relationships).
            If a node is not found, its degree will be set to 0.
        """
        if not node_ids:
            return {}

        # Format node IDs for the query
        formatted_ids = ", ".join(
            ['"' + self._normalize_node_id(node_id) + '"' for node_id in node_ids]
        )

        outgoing_query = """SELECT * FROM cypher('%s', $$
                     UNWIND [%s] AS node_id
                     MATCH (n:base {entity_id: node_id})
                     OPTIONAL MATCH (n)-[r]->(a)
                     RETURN node_id, count(a) AS out_degree
                   $$) AS (node_id text, out_degree bigint)""" % (
            self.graph_name,
            formatted_ids,
        )

        incoming_query = """SELECT * FROM cypher('%s', $$
                     UNWIND [%s] AS node_id
                     MATCH (n:base {entity_id: node_id})
                     OPTIONAL MATCH (n)<-[r]-(b)
                     RETURN node_id, count(b) AS in_degree
                   $$) AS (node_id text, in_degree bigint)""" % (
            self.graph_name,
            formatted_ids,
        )

        outgoing_results = await self._query(outgoing_query)
        incoming_results = await self._query(incoming_query)

        out_degrees = {}
        in_degrees = {}

        for result in outgoing_results:
            if result["node_id"] is not None:
                out_degrees[result["node_id"]] = int(result["out_degree"])

        for result in incoming_results:
            if result["node_id"] is not None:
                in_degrees[result["node_id"]] = int(result["in_degree"])

        degrees_dict = {}
        for node_id in node_ids:
            out_degree = out_degrees.get(node_id, 0)
            in_degree = in_degrees.get(node_id, 0)
            degrees_dict[node_id] = out_degree + in_degree

        return degrees_dict

    async def edge_degrees_batch(
        self, edges: list[tuple[str, str]]
    ) -> dict[tuple[str, str], int]:
        """
        Calculate the combined degree for each edge (sum of the source and target node degrees)
        in batch using the already implemented node_degrees_batch.

        Args:
            edges: List of (source_node_id, target_node_id) tuples

        Returns:
            Dictionary mapping edge tuples to their combined degrees
        """
        if not edges:
            return {}

        # Use node_degrees_batch to get all node degrees efficiently
        all_nodes = set()
        for src, tgt in edges:
            all_nodes.add(src)
            all_nodes.add(tgt)

        node_degrees = await self.node_degrees_batch(list(all_nodes))

        # Calculate edge degrees
        edge_degrees_dict = {}
        for src, tgt in edges:
            src_degree = node_degrees.get(src, 0)
            tgt_degree = node_degrees.get(tgt, 0)
            edge_degrees_dict[(src, tgt)] = src_degree + tgt_degree

        return edge_degrees_dict

    async def get_edges_batch(
        self, pairs: list[dict[str, str]]
    ) -> dict[tuple[str, str], dict]:
        """
        Retrieve edge properties for multiple (src, tgt) pairs in one query.
        Get forward and backward edges seperately and merge them before return

        Args:
            pairs: List of dictionaries, e.g. [{"src": "node1", "tgt": "node2"}, ...]

        Returns:
            A dictionary mapping (src, tgt) tuples to their edge properties.
        """
        if not pairs:
            return {}

        src_nodes = []
        tgt_nodes = []
        for pair in pairs:
            src_nodes.append(self._normalize_node_id(pair["src"]))
            tgt_nodes.append(self._normalize_node_id(pair["tgt"]))

        src_array = ", ".join([f'"{src}"' for src in src_nodes])
        tgt_array = ", ".join([f'"{tgt}"' for tgt in tgt_nodes])

        forward_query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                     WITH [{src_array}] AS sources, [{tgt_array}] AS targets
                     UNWIND range(0, size(sources)-1) AS i
                     MATCH (a:base {{entity_id: sources[i]}})-[r:DIRECTED]->(b:base {{entity_id: targets[i]}})
                     RETURN sources[i] AS source, targets[i] AS target, properties(r) AS edge_properties
                   $$) AS (source text, target text, edge_properties agtype)"""

        backward_query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                     WITH [{src_array}] AS sources, [{tgt_array}] AS targets
                     UNWIND range(0, size(sources)-1) AS i
                     MATCH (a:base {{entity_id: sources[i]}})<-[r:DIRECTED]-(b:base {{entity_id: targets[i]}})
                     RETURN sources[i] AS source, targets[i] AS target, properties(r) AS edge_properties
                   $$) AS (source text, target text, edge_properties agtype)"""

        forward_results = await self._query(forward_query)
        backward_results = await self._query(backward_query)

        edges_dict = {}

        for result in forward_results:
            if result["source"] and result["target"] and result["edge_properties"]:
                edge_props = result["edge_properties"]

                # Process string result, parse it to JSON dictionary
                if isinstance(edge_props, str):
                    try:
                        import json

                        edge_props = json.loads(edge_props)
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Failed to parse edge properties string: {edge_props}"
                        )
                        continue

                edges_dict[(result["source"], result["target"])] = edge_props

        for result in backward_results:
            if result["source"] and result["target"] and result["edge_properties"]:
                edge_props = result["edge_properties"]

                # Process string result, parse it to JSON dictionary
                if isinstance(edge_props, str):
                    try:
                        import json

                        edge_props = json.loads(edge_props)
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Failed to parse edge properties string: {edge_props}"
                        )
                        continue

                edges_dict[(result["source"], result["target"])] = edge_props

        return edges_dict

    async def get_nodes_edges_batch(
        self, node_ids: list[str]
    ) -> dict[str, list[tuple[str, str]]]:
        """
        Get all edges (both outgoing and incoming) for multiple nodes in a single batch operation.

        Args:
            node_ids: List of node IDs to get edges for

        Returns:
            Dictionary mapping node IDs to lists of (source, target) edge tuples
        """
        if not node_ids:
            return {}

        # Format node IDs for the query
        formatted_ids = ", ".join(
            ['"' + self._normalize_node_id(node_id) + '"' for node_id in node_ids]
        )

        outgoing_query = """SELECT * FROM cypher('%s', $$
                     UNWIND [%s] AS node_id
                     MATCH (n:base {entity_id: node_id})
                     OPTIONAL MATCH (n:base)-[]->(connected:base)
                     RETURN node_id, connected.entity_id AS connected_id
                   $$) AS (node_id text, connected_id text)""" % (
            self.graph_name,
            formatted_ids,
        )

        incoming_query = """SELECT * FROM cypher('%s', $$
                     UNWIND [%s] AS node_id
                     MATCH (n:base {entity_id: node_id})
                     OPTIONAL MATCH (n:base)<-[]-(connected:base)
                     RETURN node_id, connected.entity_id AS connected_id
                   $$) AS (node_id text, connected_id text)""" % (
            self.graph_name,
            formatted_ids,
        )

        outgoing_results = await self._query(outgoing_query)
        incoming_results = await self._query(incoming_query)

        nodes_edges_dict = {node_id: [] for node_id in node_ids}

        for result in outgoing_results:
            if result["node_id"] and result["connected_id"]:
                nodes_edges_dict[result["node_id"]].append(
                    (result["node_id"], result["connected_id"])
                )

        for result in incoming_results:
            if result["node_id"] and result["connected_id"]:
                nodes_edges_dict[result["node_id"]].append(
                    (result["connected_id"], result["node_id"])
                )

        return nodes_edges_dict

    async def get_all_labels(self) -> list[str]:
        """
        Get all labels (node IDs) in the graph.

        Returns:
            list[str]: A list of all labels in the graph.
        """
        query = (
            """SELECT * FROM cypher('%s', $$
                     MATCH (n:base)
                     WHERE n.entity_id IS NOT NULL
                     RETURN DISTINCT n.entity_id AS label
                     ORDER BY n.entity_id
                   $$) AS (label text)"""
            % self.graph_name
        )

        results = await self._query(query)
        labels = []
        for result in results:
            if result and isinstance(result, dict) and "label" in result:
                labels.append(result["label"])
        return labels

    async def _bfs_subgraph(
        self, node_label: str, max_depth: int, max_nodes: int
    ) -> KnowledgeGraph:
        """
        Implements a true breadth-first search algorithm for subgraph retrieval.
        This method is used as a fallback when the standard Cypher query is too slow
        or when we need to guarantee BFS ordering.

        Args:
            node_label: Label of the starting node
            max_depth: Maximum depth of the subgraph
            max_nodes: Maximum number of nodes to return

        Returns:
            KnowledgeGraph object containing nodes and edges
        """
        from collections import deque

        result = KnowledgeGraph()
        visited_nodes = set()
        visited_node_ids = set()
        visited_edges = set()
        visited_edge_pairs = set()

        # Get starting node data
        label = self._normalize_node_id(node_label)
        query = """SELECT * FROM cypher('%s', $$
                    MATCH (n:base {entity_id: "%s"})
                    RETURN id(n) as node_id, n
                  $$) AS (node_id bigint, n agtype)""" % (self.graph_name, label)

        node_result = await self._query(query)
        if not node_result or not node_result[0].get("n"):
            return result

        # Create initial KnowledgeGraphNode
        start_node_data = node_result[0]["n"]
        entity_id = start_node_data["properties"]["entity_id"]
        internal_id = str(start_node_data["id"])

        start_node = KnowledgeGraphNode(
            id=internal_id,
            labels=[entity_id],
            properties=start_node_data["properties"],
        )

        # Initialize BFS queue, each element is a tuple of (node, depth)
        queue = deque([(start_node, 0)])

        visited_nodes.add(entity_id)
        visited_node_ids.add(internal_id)
        result.nodes.append(start_node)

        result.is_truncated = False

        # BFS search main loop
        while queue:
            # Get all nodes at the current depth
            current_level_nodes = []
            current_depth = None

            # Determine current depth
            if queue:
                current_depth = queue[0][1]

            # Extract all nodes at current depth from the queue
            while queue and queue[0][1] == current_depth:
                node, depth = queue.popleft()
                if depth > max_depth:
                    continue
                current_level_nodes.append(node)

            if not current_level_nodes:
                continue

            # Check depth limit
            if current_depth > max_depth:
                continue

            # Prepare node IDs list
            node_ids = [node.labels[0] for node in current_level_nodes]
            formatted_ids = ", ".join(
                [f'"{self._normalize_node_id(node_id)}"' for node_id in node_ids]
            )

            # Construct batch query for outgoing edges
            outgoing_query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                UNWIND [{formatted_ids}] AS node_id
                MATCH (n:base {{entity_id: node_id}})
                OPTIONAL MATCH (n)-[r]->(neighbor:base)
                RETURN node_id AS current_id,
                       id(n) AS current_internal_id,
                       id(neighbor) AS neighbor_internal_id,
                       neighbor.entity_id AS neighbor_id,
                       id(r) AS edge_id,
                       r,
                       neighbor,
                       true AS is_outgoing
              $$) AS (current_id text, current_internal_id bigint, neighbor_internal_id bigint,
                      neighbor_id text, edge_id bigint, r agtype, neighbor agtype, is_outgoing bool)"""

            # Construct batch query for incoming edges
            incoming_query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                UNWIND [{formatted_ids}] AS node_id
                MATCH (n:base {{entity_id: node_id}})
                OPTIONAL MATCH (n)<-[r]-(neighbor:base)
                RETURN node_id AS current_id,
                       id(n) AS current_internal_id,
                       id(neighbor) AS neighbor_internal_id,
                       neighbor.entity_id AS neighbor_id,
                       id(r) AS edge_id,
                       r,
                       neighbor,
                       false AS is_outgoing
              $$) AS (current_id text, current_internal_id bigint, neighbor_internal_id bigint,
                      neighbor_id text, edge_id bigint, r agtype, neighbor agtype, is_outgoing bool)"""

            # Execute queries
            outgoing_results = await self._query(outgoing_query)
            incoming_results = await self._query(incoming_query)

            # Combine results
            neighbors = outgoing_results + incoming_results

            # Create mapping from node ID to node object
            node_map = {node.labels[0]: node for node in current_level_nodes}

            # Process all results in a single loop
            for record in neighbors:
                if not record.get("neighbor") or not record.get("r"):
                    continue

                # Get current node information
                current_entity_id = record["current_id"]
                current_node = node_map[current_entity_id]

                # Get neighbor node information
                neighbor_entity_id = record["neighbor_id"]
                neighbor_internal_id = str(record["neighbor_internal_id"])
                is_outgoing = record["is_outgoing"]

                # Determine edge direction
                if is_outgoing:
                    source_id = current_node.id
                    target_id = neighbor_internal_id
                else:
                    source_id = neighbor_internal_id
                    target_id = current_node.id

                if not neighbor_entity_id:
                    continue

                # Get edge and node information
                b_node = record["neighbor"]
                rel = record["r"]
                edge_id = str(record["edge_id"])

                # Create neighbor node object
                neighbor_node = KnowledgeGraphNode(
                    id=neighbor_internal_id,
                    labels=[neighbor_entity_id],
                    properties=b_node["properties"],
                )

                # Sort entity_ids to ensure (A,B) and (B,A) are treated as the same edge
                sorted_pair = tuple(sorted([current_entity_id, neighbor_entity_id]))

                # Create edge object
                edge = KnowledgeGraphEdge(
                    id=edge_id,
                    type=rel["label"],
                    source=source_id,
                    target=target_id,
                    properties=rel["properties"],
                )

                if neighbor_internal_id in visited_node_ids:
                    # Add backward edge if neighbor node is already visited
                    if (
                        edge_id not in visited_edges
                        and sorted_pair not in visited_edge_pairs
                    ):
                        result.edges.append(edge)
                        visited_edges.add(edge_id)
                        visited_edge_pairs.add(sorted_pair)
                else:
                    if len(visited_node_ids) < max_nodes and current_depth < max_depth:
                        # Add new node to result and queue
                        result.nodes.append(neighbor_node)
                        visited_nodes.add(neighbor_entity_id)
                        visited_node_ids.add(neighbor_internal_id)

                        # Add node to queue with incremented depth
                        queue.append((neighbor_node, current_depth + 1))

                        # Add forward edge
                        if (
                            edge_id not in visited_edges
                            and sorted_pair not in visited_edge_pairs
                        ):
                            result.edges.append(edge)
                            visited_edges.add(edge_id)
                            visited_edge_pairs.add(sorted_pair)
                    else:
                        if current_depth < max_depth:
                            result.is_truncated = True

        return result

    async def get_knowledge_graph(
        self,
        node_label: str,
        max_depth: int = 3,
        max_nodes: int = MAX_GRAPH_NODES,
    ) -> KnowledgeGraph:
        """
        Retrieve a connected subgraph of nodes where the label includes the specified `node_label`.

        Args:
            node_label: Label of the starting node, * means all nodes
            max_depth: Maximum depth of the subgraph, Defaults to 3
            max_nodes: Maxiumu nodes to return, Defaults to 1000

        Returns:
            KnowledgeGraph object containing nodes and edges, with an is_truncated flag
            indicating whether the graph was truncated due to max_nodes limit
        """
        kg = KnowledgeGraph()

        # Handle wildcard query - get all nodes
        if node_label == "*":
            # First check total node count to determine if graph should be truncated
            count_query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                    MATCH (n:base)
                    RETURN count(distinct n) AS total_nodes
                    $$) AS (total_nodes bigint)"""

            count_result = await self._query(count_query)
            total_nodes = count_result[0]["total_nodes"] if count_result else 0
            is_truncated = total_nodes > max_nodes

            # Get max_nodes with highest degrees
            query_nodes = f"""SELECT * FROM cypher('{self.graph_name}', $$
                    MATCH (n:base)
                    OPTIONAL MATCH (n)-[r]->()
                    RETURN id(n) as node_id, count(r) as degree
                $$) AS (node_id BIGINT, degree BIGINT)
                ORDER BY degree DESC
                LIMIT {max_nodes}"""
            node_results = await self._query(query_nodes)

            node_ids = [str(result["node_id"]) for result in node_results]

            logger.info(f"Total nodes: {total_nodes}, Selected nodes: {len(node_ids)}")

            if node_ids:
                formatted_ids = ", ".join(node_ids)
                # Construct batch query for subgraph within max_nodes
                query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                        WITH [{formatted_ids}] AS node_ids
                        MATCH (a)
                        WHERE id(a) IN node_ids
                        OPTIONAL MATCH (a)-[r]->(b)
                            WHERE id(b) IN node_ids
                        RETURN a, r, b
                    $$) AS (a AGTYPE, r AGTYPE, b AGTYPE)"""
                results = await self._query(query)

                # Process query results, deduplicate nodes and edges
                nodes_dict = {}
                edges_dict = {}
                for result in results:
                    # Process node a
                    if result.get("a") and isinstance(result["a"], dict):
                        node_a = result["a"]
                        node_id = str(node_a["id"])
                        if node_id not in nodes_dict and "properties" in node_a:
                            nodes_dict[node_id] = KnowledgeGraphNode(
                                id=node_id,
                                labels=[node_a["properties"]["entity_id"]],
                                properties=node_a["properties"],
                            )

                    # Process node b
                    if result.get("b") and isinstance(result["b"], dict):
                        node_b = result["b"]
                        node_id = str(node_b["id"])
                        if node_id not in nodes_dict and "properties" in node_b:
                            nodes_dict[node_id] = KnowledgeGraphNode(
                                id=node_id,
                                labels=[node_b["properties"]["entity_id"]],
                                properties=node_b["properties"],
                            )

                    # Process edge r
                    if result.get("r") and isinstance(result["r"], dict):
                        edge = result["r"]
                        edge_id = str(edge["id"])
                        if edge_id not in edges_dict:
                            edges_dict[edge_id] = KnowledgeGraphEdge(
                                id=edge_id,
                                type=edge["label"],
                                source=str(edge["start_id"]),
                                target=str(edge["end_id"]),
                                properties=edge["properties"],
                            )

                kg = KnowledgeGraph(
                    nodes=list(nodes_dict.values()),
                    edges=list(edges_dict.values()),
                    is_truncated=is_truncated,
                )
            else:
                # For single node query, use BFS algorithm
                kg = await self._bfs_subgraph(node_label, max_depth, max_nodes)

            logger.info(
                f"Subgraph query successful | Node count: {len(kg.nodes)} | Edge count: {len(kg.edges)}"
            )
        else:
            # For non-wildcard queries, use the BFS algorithm
            kg = await self._bfs_subgraph(node_label, max_depth, max_nodes)
            logger.info(
                f"Subgraph query for '{node_label}' successful | Node count: {len(kg.nodes)} | Edge count: {len(kg.edges)}"
            )

        return kg

    async def drop(self) -> dict[str, str]:
        """Drop the storage"""
        try:
            drop_query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                              MATCH (n)
                              DETACH DELETE n
                            $$) AS (result agtype)"""

            await self._query(drop_query, readonly=False)
            return {"status": "success", "message": "graph data dropped"}
        except Exception as e:
            logger.error(f"Error dropping graph: {e}")
            return {"status": "error", "message": str(e)}


NAMESPACE_TABLE_MAP = {
    NameSpace.KV_STORE_FULL_DOCS: "LIGHTRAG_DOC_FULL",
    NameSpace.KV_STORE_TEXT_CHUNKS: "LIGHTRAG_DOC_CHUNKS",
    NameSpace.VECTOR_STORE_CHUNKS: "LIGHTRAG_DOC_CHUNKS",
    NameSpace.VECTOR_STORE_ENTITIES: "LIGHTRAG_VDB_ENTITY",
    NameSpace.VECTOR_STORE_RELATIONSHIPS: "LIGHTRAG_VDB_RELATION",
    NameSpace.DOC_STATUS: "LIGHTRAG_DOC_STATUS",
    NameSpace.KV_STORE_LLM_RESPONSE_CACHE: "LIGHTRAG_LLM_CACHE",
}


def namespace_to_table_name(namespace: str) -> str:
    for k, v in NAMESPACE_TABLE_MAP.items():
        if is_namespace(namespace, k):
            return v


TABLES = {
    "LIGHTRAG_DOC_FULL": {
        "ddl": """CREATE TABLE LIGHTRAG_DOC_FULL (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    doc_name VARCHAR(1024),
                    content TEXT,
                    meta JSONB,
                    create_time TIMESTAMP(0),
                    update_time TIMESTAMP(0),
	                CONSTRAINT LIGHTRAG_DOC_FULL_PK PRIMARY KEY (workspace, id)
                    )"""
    },
    "LIGHTRAG_DOC_CHUNKS": {
        "ddl": """CREATE TABLE LIGHTRAG_DOC_CHUNKS (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    full_doc_id VARCHAR(256),
                    chunk_order_index INTEGER,
                    tokens INTEGER,
                    content TEXT,
                    content_vector VECTOR,
                    file_path VARCHAR(256),
                    create_time TIMESTAMP(0) WITH TIME ZONE,
                    update_time TIMESTAMP(0) WITH TIME ZONE,
	                CONSTRAINT LIGHTRAG_DOC_CHUNKS_PK PRIMARY KEY (workspace, id)
                    )"""
    },
    "LIGHTRAG_VDB_ENTITY": {
        "ddl": """CREATE TABLE LIGHTRAG_VDB_ENTITY (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    entity_name VARCHAR(255),
                    content TEXT,
                    content_vector VECTOR,
                    create_time TIMESTAMP(0) WITH TIME ZONE,
                    update_time TIMESTAMP(0) WITH TIME ZONE,
                    chunk_ids VARCHAR(255)[] NULL,
                    file_path TEXT NULL,
	                CONSTRAINT LIGHTRAG_VDB_ENTITY_PK PRIMARY KEY (workspace, id)
                    )"""
    },
    "LIGHTRAG_VDB_RELATION": {
        "ddl": """CREATE TABLE LIGHTRAG_VDB_RELATION (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    source_id VARCHAR(256),
                    target_id VARCHAR(256),
                    content TEXT,
                    content_vector VECTOR,
                    create_time TIMESTAMP(0) WITH TIME ZONE,
                    update_time TIMESTAMP(0) WITH TIME ZONE,
                    chunk_ids VARCHAR(255)[] NULL,
                    file_path TEXT NULL,
	                CONSTRAINT LIGHTRAG_VDB_RELATION_PK PRIMARY KEY (workspace, id)
                    )"""
    },
    "LIGHTRAG_LLM_CACHE": {
        "ddl": """CREATE TABLE LIGHTRAG_LLM_CACHE (
	                workspace varchar(255) NOT NULL,
	                id varchar(255) NOT NULL,
	                mode varchar(32) NOT NULL,
                    original_prompt TEXT,
                    return_value TEXT,
                    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP,
	                CONSTRAINT LIGHTRAG_LLM_CACHE_PK PRIMARY KEY (workspace, mode, id)
                    )"""
    },
    "LIGHTRAG_DOC_STATUS": {
        "ddl": """CREATE TABLE LIGHTRAG_DOC_STATUS (
	               workspace varchar(255) NOT NULL,
	               id varchar(255) NOT NULL,
	               content TEXT NULL,
	               content_summary varchar(255) NULL,
	               content_length int4 NULL,
	               chunks_count int4 NULL,
	               status varchar(64) NULL,
	               file_path TEXT NULL,
	               created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NULL,
	               updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NULL,
	               CONSTRAINT LIGHTRAG_DOC_STATUS_PK PRIMARY KEY (workspace, id)
	              )"""
    },
}


SQL_TEMPLATES = {
    # SQL for KVStorage
    "get_by_id_full_docs": """SELECT id, COALESCE(content, '') as content
                                FROM LIGHTRAG_DOC_FULL WHERE workspace=$1 AND id=$2
                            """,
    "get_by_id_text_chunks": """SELECT id, tokens, COALESCE(content, '') as content,
                                chunk_order_index, full_doc_id, file_path
                                FROM LIGHTRAG_DOC_CHUNKS WHERE workspace=$1 AND id=$2
                            """,
    "get_by_id_llm_response_cache": """SELECT id, original_prompt, COALESCE(return_value, '') as "return", mode
                                FROM LIGHTRAG_LLM_CACHE WHERE workspace=$1 AND mode=$2
                               """,
    "get_by_mode_id_llm_response_cache": """SELECT id, original_prompt, COALESCE(return_value, '') as "return", mode
                           FROM LIGHTRAG_LLM_CACHE WHERE workspace=$1 AND mode=$2 AND id=$3
                          """,
    "get_by_ids_full_docs": """SELECT id, COALESCE(content, '') as content
                                 FROM LIGHTRAG_DOC_FULL WHERE workspace=$1 AND id IN ({ids})
                            """,
    "get_by_ids_text_chunks": """SELECT id, tokens, COALESCE(content, '') as content,
                                  chunk_order_index, full_doc_id, file_path
                                   FROM LIGHTRAG_DOC_CHUNKS WHERE workspace=$1 AND id IN ({ids})
                                """,
    "get_by_ids_llm_response_cache": """SELECT id, original_prompt, COALESCE(return_value, '') as "return", mode
                                 FROM LIGHTRAG_LLM_CACHE WHERE workspace=$1 AND mode= IN ({ids})
                                """,
    "filter_keys": "SELECT id FROM {table_name} WHERE workspace=$1 AND id IN ({ids})",
    "upsert_doc_full": """INSERT INTO LIGHTRAG_DOC_FULL (id, content, workspace)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (workspace,id) DO UPDATE
                           SET content = $2, update_time = CURRENT_TIMESTAMP
                       """,
    "upsert_llm_response_cache": """INSERT INTO LIGHTRAG_LLM_CACHE(workspace,id,original_prompt,return_value,mode)
                                      VALUES ($1, $2, $3, $4, $5)
                                      ON CONFLICT (workspace,mode,id) DO UPDATE
                                      SET original_prompt = EXCLUDED.original_prompt,
                                      return_value=EXCLUDED.return_value,
                                      mode=EXCLUDED.mode,
                                      update_time = CURRENT_TIMESTAMP
                                     """,
    "upsert_chunk": """INSERT INTO LIGHTRAG_DOC_CHUNKS (workspace, id, tokens,
                      chunk_order_index, full_doc_id, content, content_vector, file_path,
                      create_time, update_time)
                      VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                      ON CONFLICT (workspace,id) DO UPDATE
                      SET tokens=EXCLUDED.tokens,
                      chunk_order_index=EXCLUDED.chunk_order_index,
                      full_doc_id=EXCLUDED.full_doc_id,
                      content = EXCLUDED.content,
                      content_vector=EXCLUDED.content_vector,
                      file_path=EXCLUDED.file_path,
                      update_time = EXCLUDED.update_time
                     """,
    # SQL for VectorStorage
    "upsert_entity": """INSERT INTO LIGHTRAG_VDB_ENTITY (workspace, id, entity_name, content,
                      content_vector, chunk_ids, file_path, create_time, update_time)
                      VALUES ($1, $2, $3, $4, $5, $6::varchar[], $7, $8, $9)
                      ON CONFLICT (workspace,id) DO UPDATE
                      SET entity_name=EXCLUDED.entity_name,
                      content=EXCLUDED.content,
                      content_vector=EXCLUDED.content_vector,
                      chunk_ids=EXCLUDED.chunk_ids,
                      file_path=EXCLUDED.file_path,
                      update_time=EXCLUDED.update_time
                     """,
    "upsert_relationship": """INSERT INTO LIGHTRAG_VDB_RELATION (workspace, id, source_id,
                      target_id, content, content_vector, chunk_ids, file_path, create_time, update_time)
                      VALUES ($1, $2, $3, $4, $5, $6, $7::varchar[], $8, $9, $10)
                      ON CONFLICT (workspace,id) DO UPDATE
                      SET source_id=EXCLUDED.source_id,
                      target_id=EXCLUDED.target_id,
                      content=EXCLUDED.content,
                      content_vector=EXCLUDED.content_vector,
                      chunk_ids=EXCLUDED.chunk_ids,
                      file_path=EXCLUDED.file_path,
                      update_time = EXCLUDED.update_time
                     """,
    "relationships": """
    WITH relevant_chunks AS (
        SELECT id as chunk_id
        FROM LIGHTRAG_DOC_CHUNKS
        WHERE $2::varchar[] IS NULL OR full_doc_id = ANY($2::varchar[])
    )
    SELECT source_id as src_id, target_id as tgt_id, EXTRACT(EPOCH FROM create_time)::BIGINT as created_at
    FROM (
        SELECT r.id, r.source_id, r.target_id, r.create_time, 1 - (r.content_vector <=> '[{embedding_string}]'::vector) as distance
        FROM LIGHTRAG_VDB_RELATION r
        JOIN relevant_chunks c ON c.chunk_id = ANY(r.chunk_ids)
        WHERE r.workspace=$1
    ) AS filtered
    WHERE distance>$3
    ORDER BY distance DESC
    LIMIT $4
    """,
    "entities": """
        WITH relevant_chunks AS (
            SELECT id as chunk_id
            FROM LIGHTRAG_DOC_CHUNKS
            WHERE $2::varchar[] IS NULL OR full_doc_id = ANY($2::varchar[])
        )
        SELECT entity_name, EXTRACT(EPOCH FROM create_time)::BIGINT as created_at FROM
            (
                SELECT e.id, e.entity_name, e.create_time, 1 - (e.content_vector <=> '[{embedding_string}]'::vector) as distance
                FROM LIGHTRAG_VDB_ENTITY e
                JOIN relevant_chunks c ON c.chunk_id = ANY(e.chunk_ids)
                WHERE e.workspace=$1
            ) as chunk_distances
            WHERE distance>$3
            ORDER BY distance DESC
            LIMIT $4
    """,
    "chunks": """
        WITH relevant_chunks AS (
            SELECT id as chunk_id
            FROM LIGHTRAG_DOC_CHUNKS
            WHERE $2::varchar[] IS NULL OR full_doc_id = ANY($2::varchar[])
        )
        SELECT id, content, file_path, EXTRACT(EPOCH FROM create_time)::BIGINT as created_at FROM
            (
                SELECT id, content, file_path, create_time, 1 - (content_vector <=> '[{embedding_string}]'::vector) as distance
                FROM LIGHTRAG_DOC_CHUNKS
                WHERE workspace=$1
                AND id IN (SELECT chunk_id FROM relevant_chunks)
            ) as chunk_distances
            WHERE distance>$3
            ORDER BY distance DESC
            LIMIT $4
    """,
    # DROP tables
    "drop_specifiy_table_workspace": """
        DELETE FROM {table_name} WHERE workspace=$1
       """,
}
