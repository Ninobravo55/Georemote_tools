from .geomaticape_algorithm import GeomaticapeAlgorithm
# -*- coding: utf-8 -*-
"""
Factor de escala MODIS 09 - Reflectancia Superficial
Geomaticape Plugin - Conversion
Autor: GEOMATICA AMBIENTAL  |  Version: 1.3

Productos: MOD09GQ MYD09GQ MOD09Q1 MYD09Q1  →  250m (B1-B2)
           MOD09GA MYD09GA MOD09A1 MYD09A1  →  500m (B1-B7)

Factor: ND × 0.0001
Nodata entrada: -28672  →  salida: -2.8672
Salida: WGS84 EPSG:4326 / GeoTIFF comprimido
Por cada archivo HDF se crea una subcarpeta con su nombre base.
"""

import os
from qgis.core import (
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
)
from qgis import processing


PREFIJOS_MODIS09 = [
    "MOD09GQ", "MYD09GQ", "MOD09Q1", "MYD09Q1",
    "MOD09GA", "MYD09GA", "MOD09A1", "MYD09A1",
]

# Productos de 250 m → solo B1 y B2
PREFIJOS_250M = {"MOD09GQ", "MYD09GQ", "MOD09Q1", "MYD09Q1"}

# Productos de 500 m → B1 a B7
PREFIJOS_500M = {"MOD09GA", "MYD09GA", "MOD09A1", "MYD09A1"}

# Todas las bandas posibles de reflectancia superficial MODIS 09
# Clave: fragmento que aparece en el path/descripción del SDS (case-insensitive)
# Valor: sufijo del archivo de salida
BANDAS_MODIS09_250M = [
    ("sur_refl_b01", "_B1_Red"),
    ("sur_refl_b02", "_B2_NIR"),
]

BANDAS_MODIS09_500M = [
    ("sur_refl_b01", "_B1_Red"),
    ("sur_refl_b02", "_B2_NIR"),
    ("sur_refl_b03", "_B3_Blue"),
    ("sur_refl_b04", "_B4_Green"),
    ("sur_refl_b05", "_B5_SWIR1"),
    ("sur_refl_b06", "_B6_SWIR2"),
    ("sur_refl_b07", "_B7_SWIR3"),
]

FACTOR = 0.0001
OFFSET = 0.0
NODATA_OUT = -2.8672
DTYPE_OUT = "float32"
RESAMPLE_NN = False


def _detectar_prefijo(nombre_hdf):
    """Retorna el prefijo MODIS09 del nombre del archivo, o None si no coincide."""
    nombre_upper = nombre_hdf.upper()
    for p in PREFIJOS_MODIS09:
        if nombre_upper.startswith(p):
            return p
    return None


def _buscar_sds(sds_lista, nombre_banda):
    """
    Busca en la lista de (path_sds, desc) el SDS cuyo path o descripción
    contiene el nombre de banda (comparación case-insensitive).
    Retorna el path_sds o None.
    """
    nombre_lower = nombre_banda.lower()
    for sds_path, sds_desc in sds_lista:
        if nombre_lower in sds_path.lower() or nombre_lower in sds_desc.lower():
            return sds_path
    return None


class FactorMODIS09(GeomaticapeAlgorithm):
    _algorithm_name = "factor_modis09"
    _icon_name = "indices.png"

    INPUT_FOLDER = "INPUT_FOLDER"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"

    def displayName(self):
        return self.tr("Factor escala MODIS 09 (Reflectancia Superficial)")

    def group(self):
        return self.tr("Conversion")

    def groupId(self): return "geomaticape_conversion"

    def shortHelpString(self):
        return """
<h3>Factor de escala MODIS 09 — Reflectancia Superficial</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape &nbsp;|&nbsp; <b>Versión:</b> 1.3<br><br>
<b>Productos soportados:</b>
<ul>
<li>MOD09GQ / MYD09GQ → 250 m diario (B1-B2)</li>
<li>MOD09Q1 / MYD09Q1 → 250 m 8 días (B1-B2)</li>
<li>MOD09GA / MYD09GA → 500 m diario (B1-B7)</li>
<li>MOD09A1 / MYD09A1 → 500 m 8 días (B1-B7)</li>
</ul>
<b>Factor:</b> ND × 0.0001 &nbsp;|&nbsp;
<b>Nodata salida:</b> −2.8672 &nbsp;|&nbsp;
<b>Proyección:</b> WGS84 EPSG:4326<br><br>
<b>Organización:</b> Por cada archivo HDF se crea una subcarpeta con el nombre base del producto.<br>
<b>Salidas 250 m:</b> _B1_Red, _B2_NIR<br>
<b>Salidas 500 m:</b> _B1_Red, _B2_NIR, _B3_Blue, _B4_Green, _B5_SWIR1, _B6_SWIR2, _B7_SWIR3<br>
<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_FOLDER,
            self.tr(
                "Carpeta con archivos MODIS 09 HDF\n(MOD09GQ, MYD09GQ, MOD09GA, MOD09A1, ...)"),
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

        # Filtrar solo los HDF que corresponden a productos MODIS 09
        hdfs_validos = []
        for hdf in hdfs:
            p = _detectar_prefijo(hdf)
            if p:
                hdfs_validos.append((hdf, p))

        if not hdfs_validos:
            raise Exception(
                "No se detectó ningún producto MODIS 09 válido.\n"
                "Prefijos esperados: " + ", ".join(PREFIJOS_MODIS09)
            )

        feedback.pushInfo(
            f"📄 Archivos MODIS 09 encontrados: {
                len(hdfs_validos)}")
        feedback.pushInfo("")

        total_exportados = 0
        resultados = []

        for idx, (hdf, prefijo) in enumerate(hdfs_validos):
            if feedback.isCanceled():
                break

            ruta_hdf = os.path.join(carpeta_in, hdf)
            nombre_base = os.path.splitext(hdf)[0]   # nombre completo sin .hdf

            # ── Resolución y bandas según producto ──────────────────────────
            es_250m = prefijo in PREFIJOS_250M
            bandas_activas = BANDAS_MODIS09_250M if es_250m else BANDAS_MODIS09_500M
            resolucion_txt = "250 m (B1-B2)" if es_250m else "500 m (B1-B7)"

            # ── Crear subcarpeta con el nombre base del HDF ─────────────────
            subcarpeta = os.path.join(carpeta_out, nombre_base)
            os.makedirs(subcarpeta, exist_ok=True)

            feedback.pushInfo(f"{'─' * 60}")
            feedback.pushInfo(f"📄 ({idx + 1}/{len(hdfs_validos)}) {hdf}")
            feedback.pushInfo(f"   🌍 Producto   : {prefijo}")
            feedback.pushInfo(f"   📐 Resolución : {resolucion_txt}")
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
            for sp, sd in sds_lista:
                feedback.pushInfo(f"      · {sp.split(':')[-1]}")

            # ── Procesar cada banda ─────────────────────────────────────────
            bandas_ok = 0
            for nombre_sds, sufijo in bandas_activas:

                sds_path = _buscar_sds(sds_lista, nombre_sds)

                if sds_path is None:
                    feedback.pushInfo(
                        f"   ⏭ '{nombre_sds}' no encontrado en SDS")
                    continue

                nombre_tif = nombre_base + sufijo + ".tif"
                ruta_tif = os.path.join(subcarpeta, nombre_tif)

                if os.path.exists(ruta_tif):
                    feedback.pushInfo(f"   ⏩ Ya existe: {nombre_tif}")
                    resultados.append(ruta_tif)
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
                    feedback=feedback
                )
                if ok:
                    feedback.pushInfo(f"      ✔ {nombre_tif}")
                    resultados.append(ruta_tif)
                    bandas_ok += 1
                else:
                    feedback.reportError(
                        f"      ✘ Fallo al exportar {nombre_tif}", False)

            feedback.pushInfo(
                f"   ✅ {bandas_ok}/{len(bandas_activas)} banda(s) exportada(s)")
            total_exportados += bandas_ok

        feedback.pushInfo("")
        feedback.pushInfo(f"{'=' * 60}")
        feedback.pushInfo(
            f"✅ MODIS 09 — {total_exportados} archivo(s) exportado(s) en total")
        feedback.pushInfo(f"   Carpeta raíz: {carpeta_out}")
        return {self.OUTPUT_FOLDER: carpeta_out}

    def run(self):
        processing.execAlgorithmDialog(self)
