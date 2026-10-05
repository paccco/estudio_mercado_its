#!/usr/bin/env python
# coding: utf-8

import gc
import logging
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import pandas as pd
from jobspy import scrape_jobs

# Import relativo dentro del paquete src
from s3_uploader import subir_a_s3, verificar_conexion_s3

# Configuración del logging nativo
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("Scrapper")


def configurar_rutas() -> Tuple[Path, str]:
    """Genera las rutas de almacenamiento dentro de la carpeta del scrapper."""
    ahora = datetime.now()
    fecha_hoy = ahora.strftime("%Y-%m-%d")
    timestamp = ahora.strftime("%H-%M-%S")

    # Localizado en: scrapper/scraps/YYYY-MM-DD/
    directorio_scrapper = Path(__file__).resolve().parent.parent
    ruta_carpeta = directorio_scrapper / "scraps"
    ruta_carpeta.mkdir(parents=True, exist_ok=True)

    nombre_archivo = f"ofertas_it_{fecha_hoy}_at_{timestamp}.csv"
    ruta_final = ruta_carpeta / nombre_archivo
    return ruta_final, nombre_archivo


def guardar_a_csv(df: pd.DataFrame, path: Path) -> bool:
    """Escribe incrementalmente el DataFrame en CSV."""
    if df.empty:
        return False
    df.to_csv(path, mode="a", index=False, header=not path.exists(), encoding="utf-8")
    return True


def realizar_busqueda(
    term: str,
    loc: str,
    sites: List[str],
    is_remote: bool,
    results_wanted: int,
    ruta_final: Path
) -> None:
    """Extrae las ofertas de empleo y las añade al CSV en disco."""
    tipo = "remoto" if is_remote else f"en {loc}"
    try:
        jobs = scrape_jobs(
            site_name=sites,
            search_term=term,
            location=loc,
            results_wanted=results_wanted,
            hours_old=24,
            is_remote=is_remote,
            linkedin_fetch_description=True
        )

        df_res = pd.DataFrame(jobs)
        if not df_res.empty:
            df_res["search_location"] = "Remote (Spain)" if is_remote else loc
            df_res["search_query"] = term

            if guardar_a_csv(df_res, ruta_final):
                logger.info("Guardadas %d ofertas (%s) para '%s'", len(df_res), tipo, term)
        else:
            logger.warning("Sin resultados (%s) para '%s'", tipo, term)

    except Exception as e:
        logger.error("Error durante scraping de '%s' (%s): %s", term, tipo, e)


def limpiar_duplicados(ruta_final: Path) -> None:
    """Deduplica el archivo CSV final generado."""
    if not ruta_final.exists():
        logger.warning("El archivo %s no existe para desduplicar.", ruta_final)
        return

    logger.info("Iniciando deduplicación en %s ...", ruta_final.name)
    try:
        df = pd.read_csv(ruta_final)
        total_inicial = len(df)

        df.drop_duplicates(subset=["job_url"], inplace=True)
        df.drop_duplicates(subset=["title", "company", "location"], keep="first", inplace=True)

        df.to_csv(ruta_final, index=False, encoding="utf-8")
        logger.info("Deduplicación lista: %d -> %d registros únicos.", total_inicial, len(df))
    except Exception as e:
        logger.error("Error limpiando duplicados: %s", e)


def main() -> None:
    """Orquestador principal."""
    ruta_final, nombre_archivo = configurar_rutas()
    is_test = os.getenv("TEST_MODE", "0") == "1"

    if is_test:
        logger.info("=== EJECUCIÓN MODO TEST (CI) ===")
        sites = ["indeed"]
        search_terms = ["Python Developer"]
        search_tasks = [(False, "Madrid")]
        results_wanted = 2
        delay_range = (1, 2)
    else:
        logger.info("=== EJECUCIÓN MODO PRODUCCIÓN ===")
        sites = ["linkedin", "indeed", "glassdoor"]
        search_terms = [
            "Data Engineer", "Data Analyst", "Python Developer",
            "Backend Engineer", "Software Developer", "IA Engineer"
        ]
        search_tasks = [
            (False, "Málaga"),
            (False, "Granada"),
            (False, "Sevilla"),
            (False, "Madrid"),
            (False, "Barcelona"),
            (True, "Spain")
        ]
        results_wanted = 40
        delay_range = (7, 12)

    # Obtener entorno (por defecto 'prod' si no se especifica)
    APP_ENV = os.getenv("APP_ENV", "dev").lower()
    BUCKET_BASE = "pipeline-scrapping-linkedin"
    BUCKET_NAME = f"{BUCKET_BASE}-dev" if APP_ENV == "dev" else BUCKET_BASE

    # Comprobar siempre la conexión a S3 antes de scrapear
    if not verificar_conexion_s3(BUCKET_NAME):
        logger.error("Abortando scraping: no hay conexión o permisos con el bucket S3.")
        sys.exit(1)

    # 1. Extracción de ofertas
    for is_remote, loc in search_tasks:
        logger.info("Iniciando bloque: %s", "Remoto (Spain)" if is_remote else loc)
        for term in search_terms:
            realizar_busqueda(term, loc, sites, is_remote, results_wanted, ruta_final)
            gc.collect()
            time.sleep(random.uniform(*delay_range))

    # 2. Limpieza
    limpiar_duplicados(ruta_final)

    # 3. Finalización temprana si es Smoke Test
    if is_test:
        logger.info("Smoke test completado exitosamente.")
        return

    if not ruta_final.exists():
        logger.warning("No se generó ningún CSV para persistir en S3.")
        raise Exception("No se generó ningún CSV para persistir en S3.")

    ahora = datetime.now()
    clave_s3 = f"raw/year={ahora.strftime('%Y')}/month={ahora.strftime('%m')}/{nombre_archivo}"

    if subir_a_s3(ruta_final, BUCKET_NAME, clave_s3):
        logger.info("Archivo '%s' subido a S3 con clave '%s'.", ruta_final.name, clave_s3)
    else:
        logger.error("Fallo al subir '%s' a S3.", ruta_final.name)
        raise Exception("Fallo al subir archivo a S3.")

if __name__ == "__main__":
    main()