"""Streamlit app generate SQL statements from natural language queries."""

import os
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

import pandas as pd
import sqlalchemy as sa
import streamlit as st
from langchain import OpenAI
from llama_index import (
    Document,
    GPTSimpleVectorIndex,
    GPTSQLStructStoreIndex,
    LLMPredictor,
    ServiceContext,
    SQLDatabase,
)
from llama_index.composability import ComposableGraph
from llama_index.indices.base import BaseGPTIndex
from llama_index.indices.common.struct_store.schema import SQLContextContainer
from llama_index.indices.list import GPTListIndex
from llama_index.indices.struct_store import SQLContextContainerBuilder

# from llama_index.readers import Document

SAMPLE_QUERY = "N/A"

PROMPT_TEMPLATE = (
    "Please return the relevant tables (including the full table schema) "
    "for the following query: {orig_query_str}"
)

PROMPT_TEMPLATE_IMPL = PROMPT_TEMPLATE.format(orig_query_str=SAMPLE_QUERY)


CG_QUERY_CONFIGS = [
    {
        "index_struct_type": "simple_dict",
        "query_mode": "default",
        "query_kwargs": {"similarity_top_k": 1, "verbose": True},
    },
    {"index_struct_type": "list", "query_mode": "default", "query_kwargs": {"verbose": True}},
]


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

    # build a vector index from the table schema information
    context_builder = SQLContextContainerBuilder(_sql_database)
    table_schema_index = context_builder.derive_index_from_context(
        GPTSimpleVectorIndex,
        service_context=service_context,
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
        st.markdown(
            ":blue[Query Composable Graph index of DBT sources.yaml "
            "index and table schema index]"
        )
        context_str = _context_builder.query_index_for_context(
            _index_to_query,
            query_str,
            store_context_str=True,
            # query_tmpl=PROMPT_TEMPLATE,
            query_configs=CG_QUERY_CONFIGS,
        )
    else:
        st.markdown(":blue[Query table schema index only (no DBT index)]")
        context_str = _context_builder.query_index_for_context(
            _index_to_query,
            query_str,
            store_context_str=True,
            # query_tmpl=PROMPT_TEMPLATE,
            verbose=True,
        )
    with st.expander("SQL context"):
        st.markdown(
            ":blue[Generated context for SQL query preparation:] " f":green[ {context_str} ]"
        )

    return _context_builder.build_context_container()


@st.cache_resource
def create_sql_struct_store_index(
    _sql_database: SQLDatabase, connection_string: str
) -> GPTSQLStructStoreIndex:
    """Create a SQL structure index from the SQL database.

    Args:
        _sql_database (SQLDatabase): SQL database
        connection_string (str): db_engine connection string for cache invalidation

    Returns:
        GPTSQLStructStoreIndex: SQL structure index
    """
    return GPTSQLStructStoreIndex.from_documents([], sql_database=_sql_database)


@st.cache_data(ttl=60)
def query_sql_structure_store(
    _index: GPTSQLStructStoreIndex,
    _sql_context_container: SQLContextContainer,
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
    response = _index.query(query_str, sql_context_container=_sql_context_container)

    return response


def main() -> int:
    """Start the streamlit app."""
    st.set_page_config(layout="wide")
    st.title("Natural Language to SQL Query Executor")

    # Left pane for Redshift db_engine input controls
    with st.sidebar:
        st.header("Connect to Redshift/Postgres")
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

                        query_str = st.text_area("Enter your NL query:")
                        dbt_sources_yaml_toggle = st.checkbox("Paste DBT sources.yaml")

                        dbt_sources_yaml_str = ""
                        if dbt_sources_yaml_toggle:
                            dbt_sources_yaml_str = st.text_area("Paste DBT sources.yaml:")

                        st.button("Run")

                        # query_str = SAMPLE_QUERY

                        # Execute the SQL query when 'Run' button is clicked
                        if (query_str and not dbt_sources_yaml_toggle) or (
                            query_str and dbt_sources_yaml_toggle and dbt_sources_yaml_str
                        ):
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
                                        "The table schema generated via database introspection",
                                        "DBT sources yaml file content",
                                    ],
                                )

                            # st.write(query_str)

                            # cached resource
                            sql_context_container = build_sql_context_container(
                                context_builder,
                                index_to_query,
                                query_str,
                                **cache_invalidation_triggers,
                            )

                            st.markdown(":blue[Prepare and execute query...]")
                            try:
                                # cached resource
                                index = create_sql_struct_store_index(
                                    sql_database, connection_string
                                )
                                # cached resource
                                response = query_sql_structure_store(
                                    _index=index,
                                    _sql_context_container=sql_context_container,
                                    query_str=query_str,
                                    **cache_invalidation_triggers,
                                )
                            except Exception as ex:
                                st.markdown(
                                    ":red[We couldn't generate a valid SQL query. "
                                    "Please try to refine your question with schema, "
                                    f"table or column names. Exception info:\n{ex}]"
                                )
                                return

                            st.markdown(
                                ":blue[Generated query:] "
                                f":green[_{response.extra_info['sql_query']}_]"
                            )
                            st.dataframe(pd.DataFrame(response.extra_info["result"]))

    except Exception as ex:
        st.markdown(f":red[{ex}]")
        raise ex

    return 0


# Run the Streamlit app
if __name__ == "__main__":
    main()
