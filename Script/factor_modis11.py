from .geomaticape_algorithm import GeomaticapeAlgorithm
# -*- coding: utf-8 -*-
"""
Factor de escala MODIS 11 - LST (Land Surface Temperature)
Geomaticape Plugin - Conversion
Autor: GEOMATICA AMBIENTAL  |  Version: 1.3

Cuadro 21 — Productos versión 6 de Temperatura Superficial y Emisividad:

  Clave      Temporal   Espacial  Sensor   Rango DN válido  Factor  Tipo
  ─────────  ─────────  ────────  ───────  ───────────────  ──────  ─────
  MOD11A1    Diario     1000 m    TERRA    7500 - 65535     0.02    Zona
  MOD11A2    8 días     1000 m    TERRA    7500 - 65535     0.02    Zona
  MOD11B1    Diario     5600 m    TERRA    7500 - 65535     0.02    Zona
  MOD11B2    8 días     5600 m    TERRA    7500 - 65535     0.02    Zona
  MOD11B3    Mensual    5600 m    TERRA    7500 - 65535     0.02    Zona
  MOD11C1    Diario     5600 m    TERRA    7500 - 65535     0.02    Global
  MOD11C2    8 días     5600 m    TERRA    7500 - 65535     0.02    Global
  MOD11C3    Mensual    5600 m    TERRA    7500 - 65535     0.02    Global
  MYD11A1    Diario     1000 m    AQUA     7500 - 65535     0.02    Zona
  MYD11A2    8 días     1000 m    AQUA     7500 - 65535     0.02    Zona
  MYD11B1    Diario     5600 m    AQUA     7500 - 65535     0.02    Zona
  MYD11B2    8 días     5600 m    AQUA     7500 - 65535     0.02    Zona
  MYD11B3    Mensual    5600 m    AQUA     7500 - 65535     0.02    Zona
  MYD11C1    Diario     5600 m    AQUA     7500 - 65535     0.02    Global
  MYD11C2    8 días     5600 m    AQUA     7500 - 65535     0.02    Global
  MYD11C3    Mensual    5600 m    AQUA     7500 - 65535     0.02    Global

LST(K)  = ND × 0.02
LST(°C) = ND × 0.02 − 273.15
Rango DN válido: 7500 – 65535  (valores fuera → enmascarados como nodata)
Nodata salida: -9999  |  WGS84 EPSG:4326 / GeoTIFF comprimido
Por cada archivo HDF se crea una subcarpeta con su nombre base.
"""

import os
from qgis.core import (
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
)
from qgis import processing


# ── Todos los prefijos válidos MODIS 11 (Cuadro 21) ────────────────────────
PREFIJOS_MODIS11 = [
    # TERRA
    "MOD11A1", "MOD11A2",
    "MOD11B1", "MOD11B2", "MOD11B3",
    "MOD11C1", "MOD11C2", "MOD11C3",
    # AQUA
    "MYD11A1", "MYD11A2",
    "MYD11B1", "MYD11B2", "MYD11B3",
    "MYD11C1", "MYD11C2", "MYD11C3",
]

# ── Bandas LST disponibles según resolución/producto ───────────────────────
# El nombre clave debe coincidir (case-insensitive) con el path o descripción del SDS.
# Productos A1/A2  → 1 km  → LST_Day_1km   / LST_Night_1km
# Productos B1/B2/B3 → 6 km → LST_Day_6km  / LST_Night_6km
# Productos C1/C2/C3 → CMG  → LST_Day_CMG  / LST_Night_CMG

BANDAS_LST_1KM = [
    ("LST_Day_1km", "_LST_Day_C"),
    ("LST_Night_1km", "_LST_Night_C"),
]

BANDAS_LST_6KM = [
    ("LST_Day_6km", "_LST_Day_C"),
    ("LST_Night_6km", "_LST_Night_C"),
]

BANDAS_LST_CMG = [
    ("LST_Day_CMG", "_LST_Day_C"),
    ("LST_Night_CMG", "_LST_Night_C"),
]

# Resolución por familia de producto
FAMILIA_BANDAS = {
    "A": BANDAS_LST_1KM,   # MOD/MYD11A1, A2
    "B": BANDAS_LST_6KM,   # MOD/MYD11B1, B2, B3
    "C": BANDAS_LST_CMG,   # MOD/MYD11C1, C2, C3
}

# Descripción legible de resolución para el log
FAMILIA_RESOLUCION = {
    "A": "1000 m",
    "B": "5600 m",
    "C": "CMG ~5600 m (Global)",
}

# Rango DN válido para MODIS 11 LST
DN_MIN_LST = 7500
DN_MAX_LST = 65535

FACTOR = 0.02
OFFSET = -273.15       # convierte Kelvin → Celsius
NODATA_OUT = -9999.0
DTYPE_OUT = "float32"
RESAMPLE_NN = False


def _detectar_prefijo(nombre_hdf):
    """Retorna el prefijo MODIS11 del nombre del archivo, o None si no coincide."""
    nombre_upper = nombre_hdf.upper()
    for p in PREFIJOS_MODIS11:
        if nombre_upper.startswith(p):
            return p
    return None


def _familia(prefijo):
    """Retorna 'A', 'B' o 'C' según la familia del producto."""
    # El 6.º carácter de MOD11A1 / MOD11B2 / MOD11C3 identifica la familia
    return prefijo[5].upper()   # posición 5: A, B o C


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


class FactorMODIS11(GeomaticapeAlgorithm):
    _algorithm_name = "factor_modis11"
    _icon_name = "indices.png"

    INPUT_FOLDER = "INPUT_FOLDER"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"

    def displayName(self):
        return self.tr("Factor escala MODIS 11 (LST °C)")

    def group(self):
        return self.tr("Conversion")

    def groupId(self): return "geomaticape_conversion"

    def shortHelpString(self):
        return """
<h3>Factor de escala MODIS 11 — LST Temperatura Superficial (°C)</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape &nbsp;|&nbsp; <b>Versión:</b> 1.3<br><br>
<b>Productos soportados (Cuadro 21):</b>
<ul>
<li>MOD11A1 / MYD11A1 → 1000 m diario (TERRA / AQUA)</li>
<li>MOD11A2 / MYD11A2 → 1000 m 8 días</li>
<li>MOD11B1 / MYD11B1 → 5600 m diario</li>
<li>MOD11B2 / MYD11B2 → 5600 m 8 días</li>
<li>MOD11B3 / MYD11B3 → 5600 m mensual</li>
<li>MOD11C1 / MYD11C1 → CMG diario (Global)</li>
<li>MOD11C2 / MYD11C2 → CMG 8 días (Global)</li>
<li>MOD11C3 / MYD11C3 → CMG mensual (Global)</li>
</ul>
<b>Fórmula:</b> LST(°C) = ND × 0.02 − 273.15<br>
<b>Rango DN válido:</b> 7500 – 65535 (fuera de rango → nodata)<br>
<b>Nodata salida:</b> −9999 &nbsp;|&nbsp;
<b>Proyección:</b> WGS84 EPSG:4326<br><br>
<b>Organización:</b> Por cada HDF se crea una subcarpeta con el nombre base del producto.<br>
<b>Salidas:</b> _LST_Day_C.tif, _LST_Night_C.tif<br>
<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_FOLDER,
            self.tr(
                "Carpeta con archivos MODIS 11 HDF\n(MOD11A1, MOD11A2, MOD11B1..B3, MOD11C1..C3, y variantes MYD)"),
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
                "No se detectó ningún producto MODIS 11 válido.\n"
                "Prefijos esperados: " + ", ".join(PREFIJOS_MODIS11)
            )

        feedback.pushInfo(
            f"📄 Archivos MODIS 11 encontrados: {
                len(hdfs_validos)}")
        feedback.pushInfo("")

        total_exportados = 0
        resultados = []

        for idx, (hdf, prefijo) in enumerate(hdfs_validos):
            if feedback.isCanceled():
                break

            ruta_hdf = os.path.join(carpeta_in, hdf)
            nombre_base = os.path.splitext(hdf)[0]   # nombre completo sin .hdf

            # ── Detectar familia (A=1km, B=6km, C=CMG) ─────────────────────
            fam = _familia(prefijo)
            bandas_activas = FAMILIA_BANDAS.get(fam, BANDAS_LST_1KM)
            resolucion_txt = FAMILIA_RESOLUCION.get(fam, "?")

            # ── Crear subcarpeta con el nombre base del HDF ─────────────────
            subcarpeta = os.path.join(carpeta_out, nombre_base)
            os.makedirs(subcarpeta, exist_ok=True)

            feedback.pushInfo(f"{'─' * 60}")
            feedback.pushInfo(f"📄 ({idx + 1}/{len(hdfs_validos)}) {hdf}")
            feedback.pushInfo(f"   🌡  Producto   : {prefijo}")
            feedback.pushInfo(f"   📐 Resolución : {resolucion_txt}")
            feedback.pushInfo(f"   📁 Subcarpeta : {nombre_base}")
            feedback.pushInfo(
                f"   🔢 Rango DN   : [{DN_MIN_LST}, {DN_MAX_LST}]")
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

            # ── Procesar cada banda LST ─────────────────────────────────────
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
                    feedback=feedback,
                    dn_min=DN_MIN_LST,
                    dn_max=DN_MAX_LST,
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
            f"✅ MODIS 11 — {total_exportados} archivo(s) exportado(s) en total")
        feedback.pushInfo(f"   Carpeta raíz: {carpeta_out}")
        return {self.OUTPUT_FOLDER: carpeta_out}

    def run(self):
        processing.execAlgorithmDialog(self)
