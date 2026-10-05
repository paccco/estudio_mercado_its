import os
import re
import sys
from datetime import date, datetime, timedelta
import boto3
import duckdb

def list_s3_keys_under_prefix(s3_client, bucket, prefix):
    """Recupera todas las claves bajo un prefijo en S3 manejando paginación."""
    keys = []
    paginator = s3_client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            keys.append(obj['Key'])
    return keys

def run_sync():
    # 1. Variables inyectadas directamente por GitHub Actions
    env = os.environ.get("ENV", "prod").strip().lower()
    bucket = os.environ.get("S3_BUCKET")
    token = os.environ.get("MOTHERDUCK_TOKEN")
    aws_key = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
    aws_region = os.environ.get("AWS_REGION")
    motherduck_db = os.environ.get("MOTHERDUCK_DB", f"db_{env}")

    print(f"[*] Ejecutando en entorno: {env.upper()}")
    print(f"[*] Base de datos MotherDuck: {motherduck_db}")
    print(f"[*] Bucket S3 objetivo: {bucket}")

    required_vars = {
        "S3_BUCKET": bucket,
        "MOTHERDUCK_TOKEN": token,
        "AWS_ACCESS_KEY_ID": aws_key,
        "AWS_SECRET_ACCESS_KEY": aws_secret
    }
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        print(f"[ERROR] Faltan variables obligatorias: {', '.join(missing)}")
        sys.exit(1)

    table_name = "t_scrap_offers_b"
    full_table_path = f"{motherduck_db}.bronze.{table_name}"

    # 2. Conexión y configuración de MotherDuck / S3
    con = duckdb.connect(f"md:{motherduck_db}?motherduck_token={token}")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE OR REPLACE SECRET s3_creds (
            TYPE S3,
            KEY_ID '{aws_key}',
            SECRET '{aws_secret}',
            REGION '{aws_region}'
        );
    """)
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {motherduck_db}.bronze;")

    # 3. Determinar la última fecha procesada
    check_table = con.execute(f"""
        SELECT COUNT(*) FROM information_schema.tables 
        WHERE table_catalog = '{motherduck_db}' 
          AND table_schema = 'bronze' 
          AND table_name = '{table_name}';
    """).fetchone()[0]

    already_loaded_files = set()
    if check_table == 0:
        last_date = date(2026, 1, 1)
        print(f"[*] Tabla {full_table_path} no existe. Se creará en la primera ingesta.")
    else:
        raw_last = con.execute(f"SELECT MAX(extraction_date) FROM {full_table_path};").fetchone()[0]
        if isinstance(raw_last, date):
            last_date = raw_last
        elif isinstance(raw_last, str) and raw_last:
            last_date = datetime.strptime(raw_last[:10], "%Y-%m-%d").date()
        else:
            last_date = date(2026, 1, 1)

        loaded_res = con.execute(f"SELECT DISTINCT source_file FROM {full_table_path};").fetchall()
        already_loaded_files = {row[0] for row in loaded_res if row[0]}

    start_date = last_date + timedelta(days=1)
    end_date = date.today()

    print(f"[*] Ventana de búsqueda: desde {start_date} hasta {end_date}")

    # 4. Exploración dinámica en S3
    s3_client = boto3.client(
        "s3",
        region_name=aws_region,
        aws_access_key_id=aws_key,
        aws_secret_access_key=aws_secret
    )

    target_years = range(start_date.year, end_date.year + 1)
    files_to_load = []

    for yr in target_years:
        prefix = f"raw/year={yr}/"
        s3_keys = list_s3_keys_under_prefix(s3_client, bucket, prefix)

        for key in s3_keys:
            full_s3_uri = f"s3://{bucket}/{key}"
            if full_s3_uri in already_loaded_files:
                continue

            match_month = re.search(r"raw/year=(\d{4})/month_(\d+)\.csv$", key)
            if match_month:
                f_year, f_month = int(match_month.group(1)), int(match_month.group(2))
                if f_month == 12:
                    month_end_date = date(f_year + 1, 1, 1) - timedelta(days=1)
                else:
                    month_end_date = date(f_year, f_month + 1, 1) - timedelta(days=1)

                if month_end_date >= start_date:
                    files_to_load.append(full_s3_uri)
                    print(f"  [+] Mes cerrado localizado: {key}")
                continue

            match_daily = re.search(r"ofertas_it_(\d{4}-\d{2}-\d{2})_at_.*\.csv$", key)
            if match_daily:
                f_date = datetime.strptime(match_daily.group(1), "%Y-%m-%d").date()
                if start_date <= f_date <= end_date:
                    files_to_load.append(full_s3_uri)
                    print(f"  [+] Archivo diario localizado: {key}")

    # 5. Ingesta en MotherDuck
    if not files_to_load:
        print("[*] No se encontraron nuevas particiones pendientes de carga.")
        con.close()
        return

    print(f"[*] Cargando {len(files_to_load)} archivo(s) en {full_table_path}...")

    date_parsing_sql = """
        CASE 
            WHEN regexp_matches(filename, 'ofertas_it_\\d{4}-\\d{2}-\\d{2}_at_') 
                THEN strptime(regexp_extract(filename, '(\\d{4}-\\d{2}-\\d{2})', 1), '%Y-%m-%d')::DATE
            WHEN regexp_matches(filename, 'month_\\d+\\.csv') 
                THEN strptime(
                    regexp_extract(filename, 'year=(\\d{4})', 1) || '-' || 
                    lpad(regexp_extract(filename, 'month_(\\d+)\\.csv', 1), 2, '0') || '-01',
                    '%Y-%m-%d'
                )::DATE
            ELSE CURRENT_DATE
        END AS extraction_date,
        filename AS source_file,
        CURRENT_TIMESTAMP AS ingested_at
    """

    if check_table == 0:
        con.execute(f"""
            CREATE TABLE {full_table_path} AS
            SELECT 
                *,
                {date_parsing_sql}
            FROM read_csv($1, filename = true, auto_detect = true, union_by_name = true);
        """, [files_to_load])
    else:
        # 1. Obtenemos exactamente la lista de columnas que ya tiene la tabla
        columns_desc = con.execute(f"DESCRIBE {full_table_path};").fetchall()
        existing_cols = [row[0] for row in columns_desc]
        cols_select_str = ", ".join(f'"{col}"' for col in existing_cols)

        # 2. Hacemos un CTE con la lectura completa y proyectamos SOLO las columnas existentes
        insert_query = f"""
            WITH incoming_data AS (
                SELECT 
                    *,
                    {date_parsing_sql}
                FROM read_csv($1, filename = true, auto_detect = true, union_by_name = true)
            )
            INSERT INTO {full_table_path} BY NAME
            SELECT {cols_select_str}
            FROM incoming_data
            WHERE source_file NOT IN (SELECT DISTINCT source_file FROM {full_table_path});
        """
        con.execute(insert_query, [files_to_load])

    # 6. Resumen de ejecución
    total_filas = con.execute(f"SELECT COUNT(*) FROM {full_table_path};").fetchone()[0]
    print(f"[✓] Carga completada con éxito. Total registros en {full_table_path}: {total_filas}")
    con.close()

if __name__ == "__main__":
    run_sync()