from .geomaticape_algorithm import GeomaticapeAlgorithm
# -*- coding: utf-8 -*-
"""
Factor de escala MODIS 13 - Índices de Vegetación (NDVI / EVI)
Geomaticape Plugin - Conversion
Autor: GEOMATICA AMBIENTAL  |  Version: 1.3

Cuadro  — Producto versión 6 del índice de vegetación (NDVI y EVI):

  Clave      Temporal   Espacial   Sensor  Factor   Rango DN  Extensión
  ─────────  ─────────  ─────────  ──────  ───────  ────────  ─────────
  MOD13A1    16 días    500 m      TERRA   0.0001   -2000 a   Zona
  MOD13A2    16 días    1000 m     TERRA   0.0001   10000     Zona
  MOD13A3    Mensual    1000 m     TERRA   0.0001            Zona
  MOD13C1    16 días    5600 m     TERRA   0.0001            Global
  MOD13C2    Mensual    5600 m     TERRA   0.0001            Global
  MOD13Q1    16 días    250 m      TERRA   0.0001            Zona
  MYD13A1    16 días    500 m      AQUA    0.0001            Zona
  MYD13A2    16 días    1000 m     AQUA    0.0001            Zona
  MYD13A3    Mensual    1000 m     AQUA    0.0001            Zona
  MYD13C1    16 días    5600 m     AQUA    0.0001            Global
  MYD13C2    Mensual    5600 m     AQUA    0.0001            Global
  MYD13Q1    16 días    250 m      AQUA    0.0001            Zona

NDVI = ND × 0.0001   EVI = ND × 0.0001
Rango DN válido: -2000 a 10000  (fuera de rango → nodata)
Nodata salida: -0.3  |  WGS84 EPSG:4326 / GeoTIFF comprimido
Por cada HDF se crea una subcarpeta con su nombre base.
"""

import os
from qgis.core import (
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
)
from qgis import processing


# ── Todos los prefijos válidos MODIS 13 (Cuadro 22) ────────────────────────
PREFIJOS_MODIS13 = [
    # TERRA
    "MOD13Q1",
    "MOD13A1", "MOD13A2", "MOD13A3",
    "MOD13C1", "MOD13C2",
    # AQUA
    "MYD13Q1",
    "MYD13A1", "MYD13A2", "MYD13A3",
    "MYD13C1", "MYD13C2",
]

# ── Descripción legible por familia ────────────────────────────────────────
INFO_PRODUCTO = {
    "MOD13Q1": "250 m / 16 días (TERRA)",
    "MOD13A1": "500 m / 16 días (TERRA)",
    "MOD13A2": "1000 m / 16 días (TERRA)",
    "MOD13A3": "1000 m / mensual (TERRA)",
    "MOD13C1": "5600 m / 16 días Global (TERRA)",
    "MOD13C2": "5600 m / mensual Global (TERRA)",
    "MYD13Q1": "250 m / 16 días (AQUA)",
    "MYD13A1": "500 m / 16 días (AQUA)",
    "MYD13A2": "1000 m / 16 días (AQUA)",
    "MYD13A3": "1000 m / mensual (AQUA)",
    "MYD13C1": "5600 m / 16 días Global (AQUA)",
    "MYD13C2": "5600 m / mensual Global (AQUA)",
}

# ── Bandas NDVI y EVI — claves de búsqueda case-insensitive en el SDS ──────
# Lista de tuplas (fragmento_en_sds, sufijo_tif)
# Se procesa hasta encontrar la primera coincidencia por sufijo (NDVI / EVI)
BANDAS_MODIS13 = [
    # NDVI — variantes según resolución / periodicidad
    ("250m 16 days NDVI", "_NDVI"),
    ("500m 16 days NDVI", "_NDVI"),
    ("1 km 16 days NDVI", "_NDVI"),
    ("1 km monthly NDVI", "_NDVI"),
    ("CMG 0.05 Deg 16 days NDVI", "_NDVI"),
    ("CMG 0.05 Deg Monthly NDVI", "_NDVI"),
    # EVI — variantes según resolución / periodicidad
    ("250m 16 days EVI", "_EVI"),
    ("500m 16 days EVI", "_EVI"),
    ("1 km 16 days EVI", "_EVI"),
    ("1 km monthly EVI", "_EVI"),
    ("CMG 0.05 Deg 16 days EVI", "_EVI"),
    ("CMG 0.05 Deg Monthly EVI", "_EVI"),
]

# ── Rango DN válido para MODIS 13 (Cuadro 22) ──────────────────────────────
DN_MIN_VI = -2000
DN_MAX_VI = 10000

FACTOR = 0.0001
OFFSET = 0.0
# valor nodata en reflectancia (-0.3 está fuera del rango físico)
NODATA_OUT = -0.3
DTYPE_OUT = "float32"
RESAMPLE_NN = False


def _detectar_prefijo(nombre_hdf):
    """Retorna el prefijo MODIS13 del nombre del archivo, o None si no coincide."""
    nombre_upper = nombre_hdf.upper()
    for p in PREFIJOS_MODIS13:
        if nombre_upper.startswith(p):
            return p
    return None


def _buscar_sds(sds_lista, nombre_banda):
    """
    Busca en la lista de (path_sds, desc) el SDS cuyo path o descripción
    contiene el nombre de banda (comparación case-insensitive).
    """
    nombre_lower = nombre_banda.lower()
    for sds_path, sds_desc in sds_lista:
        if nombre_lower in sds_path.lower() or nombre_lower in sds_desc.lower():
            return sds_path
    return None


class FactorMODIS13(GeomaticapeAlgorithm):
    _algorithm_name = "factor_modis13"
    _icon_name = "indices.png"

    INPUT_FOLDER = "INPUT_FOLDER"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"

    def displayName(self):
        return self.tr("Factor escala MODIS 13 (NDVI / EVI)")

    def group(self):
        return self.tr("Conversion")

    def groupId(self): return "geomaticape_conversion"

    def shortHelpString(self):
        return """
<h3>Factor de escala MODIS 13 — NDVI / EVI</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape &nbsp;|&nbsp; <b>Versión:</b> 1.3<br><br>
<b>Productos soportados (Cuadro 22):</b>
<ul>
<li>MOD13Q1 / MYD13Q1 → 250 m / 16 días</li>
<li>MOD13A1 / MYD13A1 → 500 m / 16 días</li>
<li>MOD13A2 / MYD13A2 → 1000 m / 16 días</li>
<li>MOD13A3 / MYD13A3 → 1000 m / mensual</li>
<li>MOD13C1 / MYD13C1 → 5600 m / 16 días (Global)</li>
<li>MOD13C2 / MYD13C2 → 5600 m / mensual (Global)</li>
</ul>
<b>Fórmula:</b> VI = ND × 0.0001<br>
<b>Rango DN válido:</b> −2000 a 10000 (fuera de rango → nodata)<br>
<b>Nodata salida:</b> −0.3 &nbsp;|&nbsp;
<b>Proyección:</b> WGS84 EPSG:4326<br><br>
<b>Organización:</b> Por cada HDF se crea una subcarpeta con el nombre base del producto.<br>
<b>Salidas:</b> _NDVI.tif, _EVI.tif<br>
<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_FOLDER,
            self.tr(
                "Carpeta con archivos MODIS 13 HDF\n(MOD13Q1, MOD13A1, MOD13A2, MOD13A3, MOD13C1, MOD13C2 y variantes MYD)"),
            behavior=QgsProcessingParameterFile.Behavior.Folder
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER,
            self.tr(
                "Carpeta raíz de salida GeoTIFF (se crean subcarpetas por producto)")
        ))

    # --------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):

        from ._modis_core import listar_sds, procesar_banda

        carpeta_in = self.parameterAsString(
            parameters, self.INPUT_FOLDER, context)
        carpeta_out = self.parameterAsString(
            parameters, self.OUTPUT_FOLDER, context)
        os.makedirs(carpeta_out, exist_ok=True)

        # ── Recopilar archivos HDF válidos ──────────────────────────────────
        hdfs = sorted([f for f in os.listdir(carpeta_in)
                      if f.lower().endswith(".hdf")])
        if not hdfs:
            raise Exception("No se encontraron archivos .hdf en la carpeta.")

        hdfs_validos = []
        for hdf in hdfs:
            p = _detectar_prefijo(hdf)
            if p:
                hdfs_validos.append((hdf, p))

        if not hdfs_validos:
            raise Exception(
                "No se detectó ningún producto MODIS 13 válido.\n"
                "Prefijos esperados: " + ", ".join(PREFIJOS_MODIS13)
            )

        feedback.pushInfo(
            f"📄 Archivos MODIS 13 encontrados: {
                len(hdfs_validos)}")
        feedback.pushInfo(
            f"🔢 Rango DN válido              : [{DN_MIN_VI}, {DN_MAX_VI}]")
        feedback.pushInfo("")

        total_exportados = 0
        resultados = []

        for idx, (hdf, prefijo) in enumerate(hdfs_validos):
            if feedback.isCanceled():
                break

            ruta_hdf = os.path.join(carpeta_in, hdf)
            nombre_base = os.path.splitext(hdf)[0]   # nombre completo sin .hdf
            resolucion = INFO_PRODUCTO.get(prefijo, prefijo)

            # ── Crear subcarpeta con el nombre base del HDF ─────────────────
            subcarpeta = os.path.join(carpeta_out, nombre_base)
            os.makedirs(subcarpeta, exist_ok=True)

            feedback.pushInfo(f"{'─' * 60}")
            feedback.pushInfo(f"📄 ({idx + 1}/{len(hdfs_validos)}) {hdf}")
            feedback.pushInfo(f"   🛰  Producto   : {prefijo}")
            feedback.pushInfo(f"   📐 Resolución : {resolucion}")
            feedback.pushInfo(f"   📁 Subcarpeta : {nombre_base}")
            feedback.setProgress(int(idx / len(hdfs_validos) * 100))

            # ── Listar subdatasets ──────────────────────────────────────────
            sds_lista = listar_sds(ruta_hdf, feedback)
            if not sds_lista:
                feedback.reportError(
                    f"   ⚠ No se pudieron leer los subdatasets de: {hdf}", False)
                continue

            feedback.pushInfo(
                f"   📋 Subdatasets disponibles ({
                    len(sds_lista)}):")
            for sp, _ in sds_lista:
                feedback.pushInfo(f"      · {sp.split(':')[-1]}")

            # ── Procesar NDVI y EVI (evitar duplicar el mismo sufijo) ───────
            sufijos_vistos = set()
            bandas_ok = 0

            for nombre_sds, sufijo in BANDAS_MODIS13:

                if sufijo in sufijos_vistos:
                    continue   # ya se exportó este índice para este HDF

                sds_path = _buscar_sds(sds_lista, nombre_sds)
                if sds_path is None:
                    continue   # esta variante de nombre no existe en el HDF

                nombre_tif = nombre_base + sufijo + ".tif"
                ruta_tif = os.path.join(subcarpeta, nombre_tif)

                if os.path.exists(ruta_tif):
                    feedback.pushInfo(f"   ⏩ Ya existe: {nombre_tif}")
                    resultados.append(ruta_tif)
                    sufijos_vistos.add(sufijo)
                    bandas_ok += 1
                    continue

                feedback.pushInfo(f"   ➤ {nombre_sds}  →  {nombre_tif}")

                ok = procesar_banda(
                    sds_path=sds_path,
                    ruta_tif_out=ruta_tif,
                    factor=FACTOR,
                    offset=OFFSET,
                    nodata_out=NODATA_OUT,
                    dtype_out=DTYPE_OUT,
                    resample_nn=RESAMPLE_NN,
                    feedback=feedback,
                    dn_min=DN_MIN_VI,
                    dn_max=DN_MAX_VI,
                )
                if ok:
                    feedback.pushInfo(f"      ✔ {nombre_tif}")
                    resultados.append(ruta_tif)
                    sufijos_vistos.add(sufijo)
                    bandas_ok += 1
                else:
                    feedback.reportError(
                        f"      ✘ Fallo al exportar {nombre_tif}", False)

            feedback.pushInfo(
                f"   ✅ {bandas_ok}/2 índice(s) exportado(s) [NDVI, EVI]")
            total_exportados += bandas_ok

        feedback.pushInfo("")
        feedback.pushInfo(f"{'=' * 60}")
        feedback.pushInfo(
            f"✅ MODIS 13 — {total_exportados} archivo(s) exportado(s) en total")
        feedback.pushInfo(f"   Carpeta raíz: {carpeta_out}")
        return {self.OUTPUT_FOLDER: carpeta_out}

    def run(self):
        processing.execAlgorithmDialog(self)
