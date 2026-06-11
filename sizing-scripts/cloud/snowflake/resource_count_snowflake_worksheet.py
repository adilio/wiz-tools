""" Count Snowflake Schemas in the Current Account """

from snowflake import snowpark

def get_databases(session: snowpark.Session):
    """ Get Snowflake Databases in this Account """
    try:
        # https://docs.snowflake.com/en/sql-reference/account-usage/databases
        # result = session.sql("SELECT DATABASE_NAME FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES WHERE DELETED IS NULL AND TYPE NOT IN ('APPLICATION', 'IMPORTED DATABASE')").collect()
        result = session.sql("SELECT DATABASE_NAME,TYPE FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES WHERE DELETED IS NULL AND TYPE != 'IMPORTED DATABASE' ORDER BY DATABASE_NAME").collect()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        print(f"Error getting databases: {ex}")
        result = []
    databases = []
    for database in result:
        databases.append(database)
    return databases

def get_schemas(session: snowpark.Session, database: str):
    """ Get Snowflake Schemas in this Database """
    try:
        # https://docs.snowflake.com/en/sql-reference/info-schema/schemata
        result = session.sql(f"SELECT CATALOG_NAME,SCHEMA_NAME,SCHEMA_OWNER,IS_TRANSIENT,IS_MANAGED_ACCESS FROM {database}.INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME").collect()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        print(f"Error getting {database} schemas: {ex}")
        result = []
    schemas = []
    for schema in result:
        # Drop INFORMATION_SCHEMA as per Wiz Inventory.
        if schema[1] == 'INFORMATION_SCHEMA':
            continue
        schemas.append(schema)
    return schemas

def main(session: snowpark.Session):
    """ Calculon Compute! """
    database_names = []
    schema_names   = []
    schema_count   = 0
    databases = get_databases(session)
    for database in databases:
        # databases is an array of tuples.
        database_name = database[0]
        database_names.append(database_name)
        schemas = get_schemas(session, database_name)
        for schema in schemas:
            schema_name = schema['SCHEMA_NAME']
            schema_count += 1
            schema_names.append(f"{database_name}.{schema_name}")
    rk = f"RESULTS ACROSS {len(database_names)} DATABASES"
    rv = "Value"
    result = session.create_dataframe([["Wiz Billable Schema Count", str(schema_count)]], schema=[rk, rv])
    result.show()
    return result
