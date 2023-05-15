"""Streamlit app generate SQL statements from natural language queries."""

import collections
import os
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

import chromadb
import pandas as pd
import sqlalchemy as sa
import streamlit as st
from langchain import OpenAI
from llama_index import (
    Document,
    GPTSQLStructStoreIndex,
    GPTVectorStoreIndex,
    LLMPredictor,
    ServiceContext,
    SQLDatabase,
)
from llama_index.composability import ComposableGraph
from llama_index.indices.base import BaseGPTIndex
from llama_index.indices.common.struct_store.schema import SQLContextContainer
from llama_index.indices.list import GPTListIndex
from llama_index.indices.struct_store import SQLContextContainerBuilder
from llama_index.prompts.prompts import TextToSQLPrompt
from llama_index.storage.storage_context import StorageContext
from llama_index.vector_stores import ChromaVectorStore
from requests_summary import SummaryBuilder
from streamlit.logger import get_logger
from streamlit_chat import message

LOGGER = get_logger(__name__)

# from llama_index.readers import Document
RS_TEXT_TO_SQL_TMPL = """You are an AWS Redshift expert. Given an input question,
first create a syntactically correct {dialect} query to run,
then look at the results of the query and return the answer to the input question.
Unless the user specifies in the question a specific number of examples
to obtain, query for at most 50 results. You can order the results to return the
most informative data in the database.
You must query only the columns that are needed to answer the question.
Wrap each column name in double quotes (") to denote them as delimited identifiers.
Pay attention to use only the column names you can see in the tables below.
Be careful to not query for columns that do not exist.
Also, pay attention to which column is in which table.

Use the following format:

Question: "Question here"
SQLQuery: "SQL Query to run"
SQLResult: "Result of the SQLQuery"
Answer: "Final answer here"

Only use the folowing tables listed below:
{schema}

Question: {query_str}
SQLQuery: """

RS_TEXT_TO_SQL_PROMPT = TextToSQLPrompt(RS_TEXT_TO_SQL_TMPL, stop_token="\nSQLResult:")


# Function to create a db_engine string
def create_connection_string(host: str, port: int, dbname: str, user: str, password: str) -> str:
    """Create a db_engine string for the database."""
    quote_plus_password = quote_plus(password)
    quote_plus_user = quote_plus(user)
    return f"redshift://{quote_plus_user}:{quote_plus_password }@{host}:{port}/{dbname}"


# Function to connect to the database
@st.cache_resource
def create_db_engine(connection_string: str) -> Any:
    """Connect to the Redshift/Postgres database."""
    try:
        return sa.create_engine(connection_string)
    except Exception as e:
        st.error(f"Error connecting to the database: {e}")
        return None


@st.cache_resource
def create_llama_db_wrapper(
    _connection: Any, schema: Optional[str] = None, **kwargs: Any
) -> SQLDatabase:
    """Create a SQLDatabase wrapper for the database.

    Args:
        _connection (sa.engine.db_engine): db_engine
        schema (Optional[str], optional): Schema name. Defaults to None.

    Returns:
        SQLDatabase: SQLDatabase wrapper for the database
    """
    return SQLDatabase(_connection, schema=schema)


@st.cache_resource
def build_table_schema_index(
    _sql_database: SQLDatabase, model_name: str, **kwargs: Any
) -> tuple[Any, Any]:
    """Build a table schema index from the SQL database.

    Args:
        _sql_database (SQLDatabase): SQL database
        model_name (str): Model name

    Returns:
        tuple[Any, Any]: table_schema_index, context_builder
    """
    llm_predictor = LLMPredictor(llm=OpenAI(temperature=0, model_name=model_name))
    service_context = ServiceContext.from_defaults(llm_predictor=llm_predictor)

    # Use ChromaDB to store the vectors for table schemas
    chroma_client = chromadb.Client()
    chroma_collection = chroma_client.create_collection("table_schema")
    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    # build a vector index from the table schema information
    context_builder = SQLContextContainerBuilder(_sql_database)
    table_schema_index = context_builder.derive_index_from_context(
        GPTVectorStoreIndex,
        service_context=service_context,
        storage_context=storage_context,
    )

    return table_schema_index, context_builder


@st.cache_resource
def build_sql_context_container(
    _context_builder: SQLContextContainerBuilder,
    _index_to_query: BaseGPTIndex,
    query_str: str,
    **kwargs: Any,
) -> SQLContextContainer:
    """Build a SQL context container from the table schema index."""
    # query_str
    # kwargs
    if kwargs.get("dbt_sources_yaml_toggle"):
        _context_builder.query_index_for_context(
            _index_to_query,
            query_str,
            store_context_str=True,
            query_configs=[
                {
                    "index_struct_type": "simple_dict",
                    "query_mode": "default",
                    "query_kwargs": {"similarity_top_k": 1, "verbose": True},
                },
                {
                    "index_struct_type": "list",
                    "query_mode": "default",
                    "query_kwargs": {"verbose": True},
                },
            ],
        )
    else:
        _context_builder.query_index_for_context(
            _index_to_query, query_str, store_context_str=True, verbose=True
        )

    return _context_builder.build_context_container()


@st.cache_resource
def create_sql_struct_store_index(
    _sql_database: SQLDatabase,
    _sql_context_container: SQLContextContainer,
    connection_string: str,
    query_str: str,
) -> GPTSQLStructStoreIndex:
    """Create a SQL structure index from the SQL database.

    Args:
        _sql_database (SQLDatabase): SQL database
        connection_string (str): db_engine connection string for cache invalidation

    Returns:
        GPTSQLStructStoreIndex: SQL structure index
    """
    return GPTSQLStructStoreIndex(
        sql_database=_sql_database, sql_context_container=_sql_context_container
    )


@st.cache_data(ttl=60)
def query_sql_structure_store(
    _index: GPTSQLStructStoreIndex,
    query_str: str,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Query the SQL structure index.

    Args:
        _index (GPTSQLStructStoreIndex): SQL structure index
        _sql_context_container (SQLContextContainer): SQL context container
        query_str (str): SQL query string
        openai_api_key (str): OpenAI API key placehoder for cache invalidation

    Returns:
        Dict[str, Any]: Query response
    """
    response = _index.as_query_engine(
        query_mode="nl",
    ).query(query_str)

    return response


def main() -> int:
    """Start the streamlit app."""
    st.set_page_config(layout="wide")
    st.title("Natural Language to SQL Query Executor")
    st.markdown(
        "_A proof-of-concept to demonstrate the power of large "
        "language models(LLMS) in rapidly and effectively extracting valuable insights from "
        "your data. We greatly appreciate your thoughts and suggestions. "
        "Visit us at www.sagedata.net._"
    )

    # Left pane for Redshift db_engine input controls
    with st.sidebar:
        st.image("docs/img/sagedata_logo.png", width=230)
        st.header("Connect to Redshift")
        db_credentials = st.secrets.get("db_credentials", {})
        host = st.text_input("Host", value=db_credentials.get("host", ""))
        port = st.number_input(
            "Port", min_value=1, max_value=65535, value=db_credentials.get("port", 5439)
        )
        dbname = st.text_input("Database name", value=db_credentials.get("database", ""))
        user = st.text_input("Username", value=db_credentials.get("username", ""))
        password = st.text_input(
            "Password", type="password", value=db_credentials.get("password", "")
        )
        schema = st.text_input(
            "Schema", disabled=True, value=db_credentials.get("schema", "public")
        )

        connect_button = st.button("Connect")

    try:
        # Connect to DB when 'Connect' is clicked
        if connect_button or st.session_state.get("connect_clicked"):
            st.session_state.connect_clicked = True
            # change label of connect button to 'Connected'
            connection_string = create_connection_string(host, port, dbname, user, password)
            db_engine = create_db_engine(connection_string=connection_string)

            # Right pane for SQL query input and execution
            if db_engine:
                openai_api_key = st.text_input(
                    "OpenAI API Key",
                    value=st.secrets.get(
                        "openai_api_key", st.session_state.get("openai_api_key", "")
                    ),
                    type="password",
                )

                btn_openai_api_key = st.button("Enter")

                if openai_api_key or btn_openai_api_key:
                    session_openapi_key = st.session_state.get("openai_api_key")

                    # keep streamlit state that openai_api_key had been entered
                    if not session_openapi_key or session_openapi_key != openai_api_key:
                        st.session_state.openai_api_key = openai_api_key

                    # openai libs access the key via this environment variable
                    os.environ["OPENAI_API_KEY"] = st.session_state.openai_api_key

                    model_name = st.selectbox(
                        "Choose OpenAI model",
                        ("_Choose a model_", "gpt-3.5-turbo", "text-davinci-003"),
                    )

                    if not model_name.startswith("_"):
                        cache_invalidation_triggers = {
                            "connection_string": connection_string,
                            "openai_api_key": openai_api_key,
                            "model_name": model_name,
                            "schema": str(schema),
                        }

                        # Create LLama DB wrapper
                        st.markdown(
                            (
                                ":blue[Create DB wrapper."
                                f" Inspect tables and views inside "
                                f":green[**_{dbname}.{schema}_**]]"
                            )
                        )

                        sql_database = create_llama_db_wrapper(
                            db_engine, **cache_invalidation_triggers
                        )

                        with st.expander(f"Discovered tables in {dbname}.{schema}"):
                            st.write(sql_database._all_tables)

                        # build llama sqlindex
                        st.markdown(":blue[Build table schema index.]")
                        table_schema_index, context_builder = build_table_schema_index(
                            sql_database, **cache_invalidation_triggers
                        )

                        query_history = st.session_state.get(
                            "query_history", collections.deque(maxlen=10)
                        )

                        if query_history:
                            for idx, msg in enumerate(query_history):
                                message(msg["user"], is_user=True, key=str(f"{idx}_user"))
                                if msg.get("generated"):
                                    message(
                                        msg.get("generated"), is_user=False, key=str(f"{idx}_sql")
                                    )
                        else:
                            st.session_state["query_history"] = query_history

                        query_str = st.text_input("Enter your NL query:")
                        dbt_sources_yaml_toggle = st.checkbox(
                            "Add DBT sources.yaml for additional context"
                        )

                        dbt_sources_yaml_str = ""
                        if dbt_sources_yaml_toggle:
                            dbt_sources_yaml_str = st.text_area("Paste your DBT sources.yaml:")

                        st.button("Run")

                        # Execute the SQL query when 'Run' button is clicked
                        if (query_str and not dbt_sources_yaml_toggle) or (
                            query_str and dbt_sources_yaml_toggle and dbt_sources_yaml_str
                        ):
                            query_history.append({"user": query_str})
                            cache_invalidation_triggers[
                                "dbt_sources_yaml_toggle"
                            ] = dbt_sources_yaml_toggle
                            cache_invalidation_triggers[
                                "dbt_sources_yaml_str"
                            ] = dbt_sources_yaml_str

                            index_to_query = table_schema_index

                            if dbt_sources_yaml_toggle:
                                metadata_index = GPTListIndex.from_documents(
                                    [Document(dbt_sources_yaml_str)]
                                )

                                # build ComposableGraph based on table schema index and
                                # DBT sources yaml index
                                index_to_query = ComposableGraph.from_indices(
                                    GPTListIndex,
                                    [table_schema_index, metadata_index],
                                    index_summaries=[
                                        "Tables' schemas generated via database introspection",
                                        "DBT sources yaml",
                                    ],
                                )

                                st.markdown(
                                    ":blue[Query Composable Graph index of DBT sources.yaml "
                                    "index and table schema index]"
                                )
                            else:
                                st.markdown(
                                    ":blue[Query table schema index generated "
                                    "via database introspection]"
                                )

                            # return a condensed summary for the last query
                            condensed_query_str = SummaryBuilder.build_summary(
                                map(lambda x: x["user"], query_history)
                            )
                            LOGGER.info("Generated condensed query: %s", condensed_query_str)

                            condensed_query_str = condensed_query_str.strip()

                            # cached resource
                            sql_context_container = build_sql_context_container(
                                context_builder,
                                index_to_query,
                                condensed_query_str,
                                **cache_invalidation_triggers,
                            )

                            with st.expander("SQL context"):
                                st.markdown(
                                    ":blue[Generated context for SQL query preparation:] "
                                    f":green[ {sql_context_container.context_str} ]"
                                )

                            # return
                            st.markdown(":blue[Prepare and execute query...]")
                            try:
                                # cached resource
                                index = create_sql_struct_store_index(
                                    sql_database,
                                    _sql_context_container=sql_context_container,
                                    connection_string=connection_string,
                                    query_str=condensed_query_str,
                                )
                                # cached resource
                                response = query_sql_structure_store(
                                    _index=index,
                                    query_str=condensed_query_str,
                                    **cache_invalidation_triggers,
                                )
                            except Exception as ex:
                                st.markdown(
                                    ":red[We couldn't generate a valid SQL query. "
                                    "Please try to refine your question with schema, "
                                    f"table or column names. Exception info:\n{ex}]"
                                )
                                return

                            # st.markdown(
                            #     ":blue[Generated query:] "
                            #     f":green[_{response.extra_info['sql_query']}_]"
                            # )

                            sql_query = response.extra_info["sql_query"]
                            query_history[-1]["generated"] = response.extra_info["sql_query"]
                            message(sql_query, key=str(f"{len(query_history)}_sql"))
                            st.dataframe(pd.DataFrame(response.extra_info["result"]))

    except Exception as ex:
        st.markdown(f":red[{ex}]")
        raise ex

    return 0


# Run the Streamlit app
if __name__ == "__main__":
    main()
