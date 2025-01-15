from fastapi import FastAPI, HTTPException, File, UploadFile, Form, Request
from pydantic import BaseModel
import logging
import argparse
import json
from typing import List, Dict, Any, Optional
from lightrag import LightRAG, QueryParam
from lightrag.llm import openai_complete_if_cache, ollama_embedding

from lightrag.utils import EmbeddingFunc
from enum import Enum
from pathlib import Path
import shutil
import aiofiles
from ascii_colors import trace_exception
import os

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware

from starlette.status import HTTP_403_FORBIDDEN

from dotenv import load_dotenv
load_dotenv()

# Constants for model information
LIGHTRAG_NAME = "lightrag"
LIGHTRAG_TAG = "latest"
LIGHTRAG_MODEL = "lightrag:latest"
LIGHTRAG_SIZE = 7365960935
LIGHTRAG_CREATED_AT = "2024-01-15T00:00:00Z"
LIGHTRAG_DIGEST = "sha256:lightrag"

async def llm_model_func(
    prompt, system_prompt=None, history_messages=[], keyword_extraction=False, **kwargs
) -> str:
    return await openai_complete_if_cache(
        "deepseek-chat",
        prompt,
        system_prompt=system_prompt,
        history_messages=history_messages,
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url=os.getenv("DEEPSEEK_ENDPOINT"),
        **kwargs,
    )

def get_default_host(binding_type: str) -> str:
    default_hosts = {
        "ollama": "http://m4.lan.znipower.com:11434",
        "lollms": "http://localhost:9600",
        "azure_openai": "https://api.openai.com/v1",
        "openai": os.getenv("DEEPSEEK_ENDPOINT"),
    }
    return default_hosts.get(
        binding_type, "http://localhost:11434"
    )  # fallback to ollama if unknown


def parse_args():
    parser = argparse.ArgumentParser(
        description="LightRAG FastAPI Server with separate working and input directories"
    )

    # Start by the bindings
    parser.add_argument(
        "--llm-binding",
        default="ollama",
        help="LLM binding to be used. Supported: lollms, ollama, openai (default: ollama)",
    )
    parser.add_argument(
        "--embedding-binding",
        default="ollama",
        help="Embedding binding to be used. Supported: lollms, ollama, openai (default: ollama)",
    )

    # Parse just these arguments first
    temp_args, _ = parser.parse_known_args()

    # Add remaining arguments with dynamic defaults for hosts
    # Server configuration
    parser.add_argument(
        "--host", default="0.0.0.0", help="Server host (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=9621, help="Server port (default: 9621)"
    )

    # Directory configuration
    parser.add_argument(
        "--working-dir",
        default="./rag_storage",
        help="Working directory for RAG storage (default: ./rag_storage)",
    )
    parser.add_argument(
        "--input-dir",
        default="./inputs",
        help="Directory containing input documents (default: ./inputs)",
    )

    # LLM Model configuration
    default_llm_host = get_default_host(temp_args.llm_binding)
    parser.add_argument(
        "--llm-binding-host",
        default=default_llm_host,
        help=f"llm server host URL (default: {default_llm_host})",
    )

    parser.add_argument(
        "--llm-model",
        default="mistral-nemo:latest",
        help="LLM model name (default: mistral-nemo:latest)",
    )

    # Embedding model configuration
    default_embedding_host = get_default_host(temp_args.embedding_binding)
    parser.add_argument(
        "--embedding-binding-host",
        default=default_embedding_host,
        help=f"embedding server host URL (default: {default_embedding_host})",
    )

    parser.add_argument(
        "--embedding-model",
        default="bge-m3:latest",
        help="Embedding model name (default: bge-m3:latest)",
    )

    def timeout_type(value):
        if value is None or value == "None":
            return None
        return int(value)

    parser.add_argument(
        "--timeout",
        default=None,
        type=timeout_type,
        help="Timeout in seconds (useful when using slow AI). Use None for infinite timeout",
    )
    # RAG configuration
    parser.add_argument(
        "--max-async", type=int, default=4, help="Maximum async operations (default: 4)"
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=32768,
        help="Maximum token size (default: 32768)",
    )
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=1024,
        help="Embedding dimensions (default: 1024)",
    )
    parser.add_argument(
        "--max-embed-tokens",
        type=int,
        default=8192,
        help="Maximum embedding token size (default: 8192)",
    )

    # Logging configuration
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )

    parser.add_argument(
        "--key",
        type=str,
        help="API key for authentication. This protects lightrag server against unauthorized access",
        default=None,
    )

    # Optional https parameters
    parser.add_argument(
        "--ssl", action="store_true", help="Enable HTTPS (default: False)"
    )
    parser.add_argument(
        "--ssl-certfile",
        default=None,
        help="Path to SSL certificate file (required if --ssl is enabled)",
    )
    parser.add_argument(
        "--ssl-keyfile",
        default=None,
        help="Path to SSL private key file (required if --ssl is enabled)",
    )
    return parser.parse_args()


class DocumentManager:
    """Handles document operations and tracking"""

    def __init__(self, input_dir: str, supported_extensions: tuple = (".txt", ".md")):
        self.input_dir = Path(input_dir)
        self.supported_extensions = supported_extensions
        self.indexed_files = set()

        # Create input directory if it doesn't exist
        self.input_dir.mkdir(parents=True, exist_ok=True)

    def scan_directory(self) -> List[Path]:
        """Scan input directory for new files"""
        new_files = []
        for ext in self.supported_extensions:
            for file_path in self.input_dir.rglob(f"*{ext}"):
                if file_path not in self.indexed_files:
                    new_files.append(file_path)
        return new_files

    def mark_as_indexed(self, file_path: Path):
        """Mark a file as indexed"""
        self.indexed_files.add(file_path)

    def is_supported_file(self, filename: str) -> bool:
        """Check if file type is supported"""
        return any(filename.lower().endswith(ext) for ext in self.supported_extensions)


# Pydantic models
class SearchMode(str, Enum):
    naive = "naive"
    local = "local"
    global_ = "global"  # 使用 global_ 因为 global 是 Python 保留关键字，但枚举值会转换为字符串 "global"
    hybrid = "hybrid"

# Ollama API compatible models
class OllamaMessage(BaseModel):
    role: str
    content: str
    images: Optional[List[str]] = None

class OllamaChatRequest(BaseModel):
    model: str = LIGHTRAG_MODEL
    messages: List[OllamaMessage]
    stream: bool = True  # 默认为流式模式
    options: Optional[Dict[str, Any]] = None

class OllamaChatResponse(BaseModel):
    model: str
    created_at: str
    message: OllamaMessage
    done: bool

class OllamaVersionResponse(BaseModel):
    version: str

class OllamaModelDetails(BaseModel):
    parent_model: str
    format: str
    family: str
    families: List[str]
    parameter_size: str
    quantization_level: str

class OllamaModel(BaseModel):
    name: str
    model: str
    size: int
    digest: str
    modified_at: str
    details: OllamaModelDetails

class OllamaTagResponse(BaseModel):
    models: List[OllamaModel]

# Original LightRAG models
class QueryRequest(BaseModel):
    query: str
    mode: SearchMode = SearchMode.hybrid
    stream: bool = False
    only_need_context: bool = False

class QueryResponse(BaseModel):
    response: str

class InsertTextRequest(BaseModel):
    text: str
    description: Optional[str] = None


class InsertResponse(BaseModel):
    status: str
    message: str
    document_count: int


def get_api_key_dependency(api_key: Optional[str]):
    if not api_key:
        # If no API key is configured, return a dummy dependency that always succeeds
        async def no_auth():
            return None

        return no_auth

    # If API key is configured, use proper authentication
    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    async def api_key_auth(api_key_header_value: str | None = Security(api_key_header)):
        if not api_key_header_value:
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="API Key required"
            )
        if api_key_header_value != api_key:
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="Invalid API Key"
            )
        return api_key_header_value

    return api_key_auth


def create_app(args):
    # Verify that bindings arer correctly setup
    if args.llm_binding not in ["lollms", "ollama", "openai"]:
        raise Exception("llm binding not supported")

    if args.embedding_binding not in ["lollms", "ollama", "openai"]:
        raise Exception("embedding binding not supported")

    # Add SSL validation
    if args.ssl:
        if not args.ssl_certfile or not args.ssl_keyfile:
            raise Exception(
                "SSL certificate and key files must be provided when SSL is enabled"
            )
        if not os.path.exists(args.ssl_certfile):
            raise Exception(f"SSL certificate file not found: {args.ssl_certfile}")
        if not os.path.exists(args.ssl_keyfile):
            raise Exception(f"SSL key file not found: {args.ssl_keyfile}")

    # Setup logging
    logging.basicConfig(
        format="%(levelname)s:%(message)s", level=getattr(logging, args.log_level)
    )

    # Check if API key is provided either through env var or args
    api_key = os.getenv("LIGHTRAG_API_KEY") or args.key

    # Initialize FastAPI
    app = FastAPI(
        title="LightRAG API",
        description="API for querying text using LightRAG with separate storage and input directories"
        + "(With authentication)"
        if api_key
        else "",
        version="1.0.1",
        openapi_tags=[{"name": "api"}],
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Create the optional API key dependency
    optional_api_key = get_api_key_dependency(api_key)

    # Create working directory if it doesn't exist
    Path(args.working_dir).mkdir(parents=True, exist_ok=True)

    # Initialize document manager
    doc_manager = DocumentManager(args.input_dir)

    # Initialize RAG
    rag = LightRAG(
        working_dir=args.working_dir,
        llm_model_func=llm_model_func,
        embedding_func=EmbeddingFunc(
            embedding_dim=1024,
            max_token_size=8192,
            func=lambda texts: ollama_embedding(
                texts, embed_model="bge-m3:latest", host="http://m4.lan.znipower.com:11434"
            ),
        ),
    )

    @app.on_event("startup")
    async def startup_event():
        """Index all files in input directory during startup"""
        try:
            new_files = doc_manager.scan_directory()
            for file_path in new_files:
                try:
                    # Use async file reading
                    async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                        content = await f.read()
                        # Use the async version of insert directly
                        await rag.ainsert(content)
                        doc_manager.mark_as_indexed(file_path)
                        logging.info(f"Indexed file: {file_path}")
                except Exception as e:
                    trace_exception(e)
                    logging.error(f"Error indexing file {file_path}: {str(e)}")

            logging.info(f"Indexed {len(new_files)} documents from {args.input_dir}")

        except Exception as e:
            logging.error(f"Error during startup indexing: {str(e)}")

    @app.post("/documents/scan", dependencies=[Depends(optional_api_key)])
    async def scan_for_new_documents():
        """Manually trigger scanning for new documents"""
        try:
            new_files = doc_manager.scan_directory()
            indexed_count = 0

            for file_path in new_files:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        await rag.ainsert(content)
                        doc_manager.mark_as_indexed(file_path)
                        indexed_count += 1
                except Exception as e:
                    logging.error(f"Error indexing file {file_path}: {str(e)}")

            return {
                "status": "success",
                "indexed_count": indexed_count,
                "total_documents": len(doc_manager.indexed_files),
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/documents/upload", dependencies=[Depends(optional_api_key)])
    async def upload_to_input_dir(file: UploadFile = File(...)):
        """Upload a file to the input directory"""
        try:
            if not doc_manager.is_supported_file(file.filename):
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported file type. Supported types: {doc_manager.supported_extensions}",
                )

            file_path = doc_manager.input_dir / file.filename
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            # Immediately index the uploaded file
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                await rag.ainsert(content)
                doc_manager.mark_as_indexed(file_path)

            return {
                "status": "success",
                "message": f"File uploaded and indexed: {file.filename}",
                "total_documents": len(doc_manager.indexed_files),
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post(
        "/query", response_model=QueryResponse, dependencies=[Depends(optional_api_key)]
    )
    async def query_text(request: QueryRequest):
        try:
            response = await rag.aquery(
                request.query,
                param=QueryParam(
                    mode=request.mode,
                    stream=request.stream,
                    only_need_context=request.only_need_context,
                ),
            )

            if request.stream:
                from fastapi.responses import StreamingResponse

                async def stream_generator():
                    async for chunk in response:
                        yield f"{json.dumps({'response': chunk})}\n"

                return StreamingResponse(
                    stream_generator(),
                    media_type="application/x-ndjson",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "Content-Type": "application/x-ndjson",
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type"
                    }
                )
            else:
                return QueryResponse(response=response)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/query/stream", dependencies=[Depends(optional_api_key)])
    async def query_text_stream(request: QueryRequest):
        try:
            response = await rag.aquery(  # 使用 aquery 而不是 query,并添加 await
                request.query,
                param=QueryParam(
                    mode=request.mode,
                    stream=True,
                    only_need_context=request.only_need_context,
                ),
            )

            from fastapi.responses import StreamingResponse

            async def stream_generator():
                async for chunk in response:
                    yield f"{json.dumps({'response': chunk})}\n"

            return StreamingResponse(
                stream_generator(),
                media_type="application/x-ndjson",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Content-Type": "application/x-ndjson",
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type"
                }
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post(
        "/documents/text",
        response_model=InsertResponse,
        dependencies=[Depends(optional_api_key)],
    )
    async def insert_text(request: InsertTextRequest):
        try:
            await rag.ainsert(request.text)
            return InsertResponse(
                status="success",
                message="Text successfully inserted",
                document_count=1,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post(
        "/documents/file",
        response_model=InsertResponse,
        dependencies=[Depends(optional_api_key)],
    )
    async def insert_file(file: UploadFile = File(...), description: str = Form(None)):
        try:
            content = await file.read()

            if file.filename.endswith((".txt", ".md")):
                text = content.decode("utf-8")
                await rag.ainsert(text)
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Unsupported file type. Only .txt and .md files are supported",
                )

            return InsertResponse(
                status="success",
                message=f"File '{file.filename}' successfully inserted",
                document_count=1,
            )
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="File encoding not supported")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post(
        "/documents/batch",
        response_model=InsertResponse,
        dependencies=[Depends(optional_api_key)],
    )
    async def insert_batch(files: List[UploadFile] = File(...)):
        try:
            inserted_count = 0
            failed_files = []

            for file in files:
                try:
                    content = await file.read()
                    if file.filename.endswith((".txt", ".md")):
                        text = content.decode("utf-8")
                        await rag.ainsert(text)
                        inserted_count += 1
                    else:
                        failed_files.append(f"{file.filename} (unsupported type)")
                except Exception as e:
                    failed_files.append(f"{file.filename} ({str(e)})")

            status_message = f"Successfully inserted {inserted_count} documents"
            if failed_files:
                status_message += f". Failed files: {', '.join(failed_files)}"

            return InsertResponse(
                status="success" if inserted_count > 0 else "partial_success",
                message=status_message,
                document_count=len(files),
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete(
        "/documents",
        response_model=InsertResponse,
        dependencies=[Depends(optional_api_key)],
    )
    async def clear_documents():
        try:
            rag.text_chunks = []
            rag.entities_vdb = None
            rag.relationships_vdb = None
            return InsertResponse(
                status="success",
                message="All documents cleared successfully",
                document_count=0,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Ollama compatible API endpoints
    @app.get("/api/version")
    async def get_version():
        """Get Ollama version information"""
        return OllamaVersionResponse(
            version="0.5.4"
        )

    @app.get("/api/tags")
    async def get_tags():
        """Get available models"""
        return OllamaTagResponse(
            models=[{
                "name": LIGHTRAG_MODEL,
                "model": LIGHTRAG_MODEL,
                "size": LIGHTRAG_SIZE,
                "digest": LIGHTRAG_DIGEST,
                "modified_at": LIGHTRAG_CREATED_AT,
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": LIGHTRAG_NAME,
                    "families": [LIGHTRAG_NAME],
                    "parameter_size": "13B",
                    "quantization_level": "Q4_0"
                }                
            }]
        )

    def parse_query_mode(query: str) -> tuple[str, SearchMode]:
        """Parse query prefix to determine search mode
        Returns tuple of (cleaned_query, search_mode)
        """
        mode_map = {
            "/local ": SearchMode.local,
            "/global ": SearchMode.global_,  # global_ is used because 'global' is a Python keyword
            "/naive ": SearchMode.naive,
            "/hybrid ": SearchMode.hybrid
        }
        
        for prefix, mode in mode_map.items():
            if query.startswith(prefix):
                return query[len(prefix):], mode
                
        return query, SearchMode.hybrid

    @app.post("/api/chat")
    async def chat(raw_request: Request, request: OllamaChatRequest):
        # 打印原始请求数据
        body = await raw_request.body()
        logging.info(f"收到 /api/chat 原始请求: {body.decode('utf-8')}")
        """Handle chat completion requests"""
        try:
            # 获取所有消息内容
            messages = request.messages
            if not messages:
                raise HTTPException(status_code=400, detail="No messages provided")
            
            # 获取最后一条消息作为查询
            query = messages[-1].content
            
            # 解析查询模式
            cleaned_query, mode = parse_query_mode(query)
            
            # 调用RAG进行查询
            query_param = QueryParam(
                mode=mode,  # 使用解析出的模式,如果没有前缀则为默认的 hybrid
                stream=request.stream,
                only_need_context=False
            )
            
            if request.stream:
                from fastapi.responses import StreamingResponse
                
                response = await rag.aquery(  # 需要 await 来获取异步生成器
                    cleaned_query,
                    param=query_param
                )

                async def stream_generator():
                    try:
                        # 确保 response 是异步生成器
                        if isinstance(response, str):
                            # 如果是字符串,分两次发送
                            # 第一次发送查询内容
                            data = {
                                "model": LIGHTRAG_MODEL,
                                "created_at": LIGHTRAG_CREATED_AT,
                                "message": {
                                    "role": "assistant", 
                                    "content": response,
                                    "images": None
                                },
                                "done": False
                            }
                            yield f"{json.dumps(data, ensure_ascii=False)}\n"
                            
                            # 第二次发送统计信息
                            data = {
                                "model": LIGHTRAG_MODEL,
                                "created_at": LIGHTRAG_CREATED_AT,
                                "done": True,
                                "total_duration": 1,
                                "load_duration": 1,
                                "prompt_eval_count": 999,
                                "prompt_eval_duration": 1,
                                "eval_count": 999,
                                "eval_duration": 1
                            }
                            yield f"{json.dumps(data, ensure_ascii=False)}\n"
                        else:
                            # 流式响应
                            async for chunk in response:
                                if chunk:  # 只发送非空内容
                                    data = {
                                        "model": LIGHTRAG_MODEL,
                                        "created_at": LIGHTRAG_CREATED_AT,
                                        "message": {
                                            "role": "assistant",
                                            "content": chunk,
                                            "images": None
                                        },
                                        "done": False
                                    }
                                    yield f"{json.dumps(data, ensure_ascii=False)}\n"
                            
                            # 发送完成标记，包含性能统计信息
                            data = {
                                "model": LIGHTRAG_MODEL,
                                "created_at": LIGHTRAG_CREATED_AT,
                                "done": True,
                                "total_duration": 1,  # 由于我们没有实际统计这些指标，暂时使用默认值
                                "load_duration": 1,
                                "prompt_eval_count": 999,
                                "prompt_eval_duration": 1,
                                "eval_count": 999,
                                "eval_duration": 1
                            }
                            yield f"{json.dumps(data, ensure_ascii=False)}\n"
                            return  # 确保生成器在发送完成标记后立即结束
                    except Exception as e:
                        logging.error(f"Error in stream_generator: {str(e)}")
                        raise
                
                return StreamingResponse(
                    stream_generator(),
                    media_type="application/x-ndjson",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "Content-Type": "application/x-ndjson",
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type"
                    }
                )
            else:
                # 非流式响应
                response_text = await rag.aquery(
                    cleaned_query,
                    param=query_param
                )
                
                # 确保响应不为空
                if not response_text:
                    response_text = "No response generated"
                    
                # 构造响应，包含性能统计信息
                return {
                    "model": LIGHTRAG_MODEL,
                    "created_at": LIGHTRAG_CREATED_AT,
                    "message": {
                        "role": "assistant",
                        "content": str(response_text),  # 确保转换为字符串
                        "images": None
                    },
                    "done": True,
                    "total_duration": 1,  # 由于我们没有实际统计这些指标，暂时使用默认值
                    "load_duration": 1,
                    "prompt_eval_count": 999,
                    "prompt_eval_duration": 1,
                    "eval_count": 999,
                    "eval_duration": 1
                }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/health", dependencies=[Depends(optional_api_key)])
    async def get_status():
        """Get current system status"""
        return {
            "status": "healthy",
            "working_directory": str(args.working_dir),
            "input_directory": str(args.input_dir),
            "indexed_files": len(doc_manager.indexed_files),
            "configuration": {
                # LLM configuration binding/host address (if applicable)/model (if applicable)
                "llm_binding": args.llm_binding,
                "llm_binding_host": args.llm_binding_host,
                "llm_model": args.llm_model,
                # embedding model configuration binding/host address (if applicable)/model (if applicable)
                "embedding_binding": args.embedding_binding,
                "embedding_binding_host": args.embedding_binding_host,
                "embedding_model": args.embedding_model,
                "max_tokens": args.max_tokens,
            },
        }

    return app


def main():
    args = parse_args()
    import uvicorn

    app = create_app(args)
    uvicorn_config = {
        "app": app,
        "host": args.host,
        "port": args.port,
    }
    if args.ssl:
        uvicorn_config.update(
            {
                "ssl_certfile": args.ssl_certfile,
                "ssl_keyfile": args.ssl_keyfile,
            }
        )
    uvicorn.run(**uvicorn_config)


if __name__ == "__main__":
    main()
