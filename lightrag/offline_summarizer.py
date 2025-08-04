
import asyncio
import os
from functools import partial

from lightrag.operate import _handle_entity_relation_summary, compute_mdhash_id
from lightrag.lightrag import LightRAG
from lightrag.base import BaseGraphStorage, BaseVectorStorage, BaseKVStorage
from lightrag.utils import logger

async def offline_summarize(
    knowledge_graph_inst: BaseGraphStorage,
    entity_vdb: BaseVectorStorage,
    relationships_vdb: BaseVectorStorage,
    llm_response_cache: BaseKVStorage,
    global_config: dict,
):
    """
    Performs offline summarization of nodes and edges in the knowledge graph
    that are marked with summary_status: 'PENDING'.
    """
    logger.info("Starting offline summarization process...")

    # Get pending nodes and edges
    pending_nodes = await knowledge_graph_inst.get_nodes_by_property("summary_status", "PENDING")
    pending_edges = await knowledge_graph_inst.get_edges_by_property("summary_status", "PENDING")

    logger.info(f"Found {len(pending_nodes)} nodes and {len(pending_edges)} edges pending summarization.")

    # Process nodes
    for node in pending_nodes:
        entity_name = node["entity_id"]
        logger.info(f"Summarizing node: {entity_name}")
        try:
            # Get the full node data
            full_node = await knowledge_graph_inst.get_node(entity_name)
            if not full_node:
                logger.warning(f"Skipping node {entity_name} as it could not be retrieved.")
                continue

            description = full_node.get("description", "")
            if not description:
                logger.warning(f"Skipping node {entity_name} due to empty description.")
                await knowledge_graph_inst.update_node_properties(entity_name, {"summary_status": "COMPLETED"})
                continue

            # Summarize
            summary = await _handle_entity_relation_summary(
                entity_name,
                description,
                global_config,
                llm_response_cache=llm_response_cache,
            )

            # Update graph database
            await knowledge_graph_inst.update_node_properties(
                entity_name,
                {"description": summary, "summary_status": "COMPLETED"},
            )

            # Update vector database
            if entity_vdb:
                vdb_id = compute_mdhash_id(entity_name, prefix="ent-")
                content_for_vdb = f"{entity_name}\n{summary}"
                await entity_vdb.update_document(vdb_id, {"content": content_for_vdb})

            logger.info(f"Successfully summarized and updated node: {entity_name}")

        except Exception as e:
            logger.error(f"Error processing node {entity_name}: {e}")
            await knowledge_graph_inst.update_node_properties(entity_name, {"summary_status": "FAILED"})


    # Process edges
    for edge in pending_edges:
        src_id, tgt_id = edge["src_id"], edge["tgt_id"]
        edge_key = f"({src_id}, {tgt_id})"
        logger.info(f"Summarizing edge: {edge_key}")
        try:
            # Get the full edge data
            full_edge = await knowledge_graph_inst.get_edge(src_id, tgt_id)
            if not full_edge:
                logger.warning(f"Skipping edge {edge_key} as it could not be retrieved.")
                continue

            description = full_edge.get("description", "")
            if not description:
                logger.warning(f"Skipping edge {edge_key} due to empty description.")
                await knowledge_graph_inst.update_edge_properties(src_id, tgt_id, {"summary_status": "COMPLETED"})
                continue

            # Summarize
            summary = await _handle_entity_relation_summary(
                edge_key,
                description,
                global_config,
                llm_response_cache=llm_response_cache,
            )

            # Update graph database
            await knowledge_graph_inst.update_edge_properties(
                src_id,
                tgt_id,
                {"description": summary, "summary_status": "COMPLETED"},
            )

            # Update vector database
            if relationships_vdb:
                vdb_id = compute_mdhash_id(src_id + tgt_id, prefix="rel-")
                content_for_vdb = f"{src_id}\t{tgt_id}\n{full_edge.get('keywords', '')}\n{summary}"
                await relationships_vdb.update_document(vdb_id, {"content": content_for_vdb})

            logger.info(f"Successfully summarized and updated edge: {edge_key}")

        except Exception as e:
            logger.error(f"Error processing edge {edge_key}: {e}")
            await knowledge_graph_inst.update_edge_properties(src_id, tgt_id, {"summary_status": "FAILED"})

    logger.info("Offline summarization process finished.")


async def main():
    # This is an example of how to run the offline summarizer.
    # You will need to configure your components (graph, vdbs, etc.) as you do in your main application.
    # It's recommended to use a configuration file (e.g., config.ini) for this.

    # Load components from a config file or set them up manually
    # Example using the LightRAG class
    config_path = os.getenv("LIGHTRAG_CONFIG_PATH", "config.ini")
    if not os.path.exists(config_path):
        logger.error(f"Configuration file not found at {config_path}.")
        logger.error("Please set the LIGHTRAG_CONFIG_PATH environment variable or place config.ini in the current directory.")
        return

    # Instantiate the LightRAG class to load all components
    lightrag_app = LightRAG(config_path=config_path)
    
    # Access the components from the instance
    knowledge_graph = lightrag_app.knowledge_graph
    entity_vdb = lightrag_app.entity_vdb
    relation_vdb = lightrag_app.relation_vdb
    llm_response_cache = lightrag_app.llm_response_cache
    global_config = lightrag_app.get_global_config()


    await offline_summarize(
        knowledge_graph_inst=knowledge_graph,
        entity_vdb=entity_vdb,
        relationships_vdb=relation_vdb,
        llm_response_cache=llm_response_cache,
        global_config=global_config,
    )

if __name__ == "__main__":
    # To run this script, you can execute `python -m lightrag.offline_summarizer` from the project root.
    # Make sure your environment is set up correctly (e.g., .env file for API keys).
    asyncio.run(main())
