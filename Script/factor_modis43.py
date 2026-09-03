from .geomaticape_algorithm import GeomaticapeAlgorithm
# -*- coding: utf-8 -*-
"""
Factor de escala MODIS 43 - MCD43 (BRDF, Albedo, NBAR, Quality)
Geomaticape Plugin - Conversion
Autor: GEOMATICA AMBIENTAL  |  Version: 1.10

Productos:
  MCD43A1 - Parámetros BRDF (500m)
  MCD43A2 - Calidad y validez BRDF (500m)
  MCD43A3 - Albedo (500m)
  MCD43A4 - NBAR (500m)

Factor: Según el producto
Salida: WGS84 EPSG:4326 / GeoTIFF comprimido
"""

import os
from qgis.core import (
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
)
from qgis import processing
from osgeo import gdal


PREFIJOS_MODIS43 = [
    "MCD43A1", "MCD43A2", "MCD43A3", "MCD43A4"
]


def _detectar_prefijo(nombre_hdf):
    """Retorna el prefijo MODIS43 del nombre del archivo, o None."""
    nombre_upper = nombre_hdf.upper()
    for p in PREFIJOS_MODIS43:
        if nombre_upper.startswith(p):
            return p
    return None


def _parametros_por_banda(sds_nombre, prefijo):
    """
    Asigna factor y tipo de dato dependiendo del producto MODIS 43 y la banda.
    """
    n = sds_nombre.lower()

    if prefijo == "MCD43A2":
        return 1.0, 0.0, -9999, "int32", 0, 65535

    # Identificar bandas de calidad, incertidumbre, días, o ángulos que no
    # usan escala
    if "qa" in n or "quality" in n or "uncertainty" in n or "local" in n or "day" in n or "snow" in n or "obs" in n:
        # Sin factor de escala, sin rango válido
        return 1.0, 0.0, -32768, "int16", None, None
    else:
        if prefijo in ("MCD43A1", "MCD43A3"):
            return 0.001, 0.0, -32768.0, "float32", 0, 32766
        elif prefijo == "MCD43A4":
            return 0.0001, 0.0, -32768.0, "float32", 0, 32766
        else:
            return 1.0, 0.0, -32768, "int16", None, None


class FactorMODIS43(GeomaticapeAlgorithm):
    _algorithm_name = "factor_modis43"
    _icon_name = "indices.png"

    INPUT_FOLDER = "INPUT_FOLDER"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"

    def displayName(self):
        return self.tr("Factor escala MODIS 43 (BRDF / Albedo / NBAR)")

    def group(self):
        return self.tr("Conversion")

    def groupId(self):
        return "geomaticape_conversion"

    def shortHelpString(self):
        return self.tr(
            "<h3>Factor de escala MODIS 43 — BRDF, Albedo y NBAR (500m)</h3>"
            "<b>Autor:</b> GEOMATICA AMBIENTAL<br>"
            "<b>Plugin:</b> Geomaticape &nbsp;|&nbsp; <b>Versión:</b> 1.10<br><br>"
            "<b>Productos soportados:</b>"
            "<ul>"
            "<li>MCD43A1 → Parámetros BRDF (Factor: 0.001, Rango: 0 a 32766)</li>"
            "<li>MCD43A2 → Calidad QA (Factor: 1, Rango: 0 a 65535)</li>"
            "<li>MCD43A3 → Albedo (Factor: 0.001, Rango: 0 a 32766)</li>"
            "<li>MCD43A4 → NBAR Reflectancia (Factor: 0.0001, Rango: 0 a 32766)</li>"
            "</ul>"
            "<b>Proyección:</b> WGS84 EPSG:4326<br><br>"
            "<b>Organización:</b> Por cada HDF se extraen TODOS sus subdatasets aplicando el factor de escala y rango válido que corresponda según su producto."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_FOLDER,
            self.tr(
                "Carpeta con archivos MODIS 43 HDF\n(MCD43A1, MCD43A2, MCD43A3, MCD43A4)"),
            behavior=QgsProcessingParameterFile.Behavior.Folder
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER,
            self.tr(
                "Carpeta raíz de salida GeoTIFF (se crean subcarpetas por producto)")
        ))

    def processAlgorithm(self, parameters, context, feedback):
        from ._modis_core import listar_sds, procesar_banda

        carpeta_in = self.parameterAsString(
            parameters, self.INPUT_FOLDER, context)
        carpeta_out = self.parameterAsString(
            parameters, self.OUTPUT_FOLDER, context)
        os.makedirs(carpeta_out, exist_ok=True)

        hdfs = sorted([f for f in os.listdir(carpeta_in)
                      if f.lower().endswith(".hdf")])
        if not hdfs:
            raise Exception(
                self.tr("No se encontraron archivos .hdf en la carpeta."))

        hdfs_validos = []
        for hdf in hdfs:
            p = _detectar_prefijo(hdf)
            if p:
                hdfs_validos.append((hdf, p))

        if not hdfs_validos:
            raise Exception(
                self.tr("No se detectó ningún producto MODIS 43 válido."))

        feedback.pushInfo(
            self.tr("📄 Archivos MODIS 43 encontrados: {}").format(
                len(hdfs_validos)))
        feedback.pushInfo("")

        total_exportados = 0

        for idx, (hdf, prefijo) in enumerate(hdfs_validos):
            if feedback.isCanceled():
                break

            ruta_hdf = os.path.join(carpeta_in, hdf)
            nombre_base = os.path.splitext(hdf)[0]

            subcarpeta = os.path.join(carpeta_out, nombre_base)
            os.makedirs(subcarpeta, exist_ok=True)

            feedback.pushInfo(f"{'─' * 60}")
            feedback.pushInfo(f"📄 ({idx + 1}/{len(hdfs_validos)}) {hdf}")
            feedback.pushInfo(f"   🌍 Producto   : {prefijo}")
            feedback.pushInfo(f"   📁 Subcarpeta : {nombre_base}")
            feedback.setProgress(int(idx / len(hdfs_validos) * 100))

            sds_lista = listar_sds(ruta_hdf, feedback)
            if not sds_lista:
                feedback.reportError(
                    self.tr("   ⚠ No se pudieron leer los subdatasets"), False)
                continue

            bandas_ok = 0
            mcd43a4_tifs = {}
            # Procesar todos los SDS
            for sds_path, sds_desc in sds_lista:
                # Nombre limpio de la banda
                nombre_sds = sds_path.split(":")[-1].replace(" ", "_")

                # Para evitar rutas excesivamente largas, si el SDS tiene un nombre gigante, lo acortamos
                # Omitir bandas de calidad según el producto
                if prefijo == "MCD43A1" and "Mandatory_Quality" in nombre_sds:
                    continue
                if prefijo == "MCD43A2" and "Quality" in nombre_sds:
                    continue
                if prefijo == "MCD43A3" and "Mandatory_Quality" in nombre_sds:
                    continue

                band_key = None
                # Agregar el nombre del color/rango a las bandas de MCD43A4
                if prefijo == "MCD43A4":
                    # Omitir las bandas Mandatory_Quality para MCD43A4
                    if "Mandatory_Quality" in nombre_sds:
                        continue

                    if "Band1" in nombre_sds:
                        band_key = "red"
                        nombre_sds += "_red"
                    elif "Band2" in nombre_sds:
                        band_key = "nir"
                        nombre_sds += "_nir"
                    elif "Band3" in nombre_sds:
                        band_key = "blue"
                        nombre_sds += "_blue"
                    elif "Band4" in nombre_sds:
                        band_key = "green"
                        nombre_sds += "_green"
                    elif "Band5" in nombre_sds:
                        band_key = "swir1"
                        nombre_sds += "_swir1"
                    elif "Band6" in nombre_sds:
                        band_key = "swir2"
                        nombre_sds += "_swir2"
                    elif "Band7" in nombre_sds:
                        band_key = "swir3"
                        nombre_sds += "_swir3"

                nombre_tif = f"{nombre_base}_{nombre_sds}.tif"
                ruta_tif = os.path.join(subcarpeta, nombre_tif)

                if os.path.exists(ruta_tif):
                    feedback.pushInfo(
                        self.tr("   ⏩ Ya existe: {}").format(nombre_tif))
                    bandas_ok += 1
                    if prefijo == "MCD43A4" and band_key:
                        mcd43a4_tifs[band_key] = ruta_tif
                    continue

                fct, off, nodata_o, dtype_o, dmin, dmax = _parametros_por_banda(
                    nombre_sds, prefijo)

                feedback.pushInfo(
                    self.tr("   ➤ {}  →  factor: {}").format(
                        nombre_sds, fct))

                ok = procesar_banda(
                    sds_path=sds_path,
                    ruta_tif_out=ruta_tif,
                    factor=fct,
                    offset=off,
                    nodata_out=nodata_o,
                    dtype_out=dtype_o,
                    resample_nn=False,
                    feedback=feedback,
                    dn_min=dmin,
                    dn_max=dmax
                )

                if ok:
                    feedback.pushInfo(self.tr("      ✔ {}").format(nombre_tif))
                    bandas_ok += 1
                    if prefijo == "MCD43A4" and band_key:
                        mcd43a4_tifs[band_key] = ruta_tif
                else:
                    feedback.reportError(
                        self.tr("      ✘ Fallo al exportar {}").format(nombre_tif), False)

            # Si es MCD43A4, hacer composición
            if prefijo == "MCD43A4" and len(mcd43a4_tifs) == 7:
                feedback.pushInfo(
                    self.tr("   🛰 Generando composición de bandas MCD43A4..."))
                order = [
                    "blue",
                    "green",
                    "red",
                    "nir",
                    "swir1",
                    "swir2",
                    "swir3"]
                tifs_to_merge = [mcd43a4_tifs[k]
                                 for k in order if k in mcd43a4_tifs]

                comp_tif = os.path.join(
                    subcarpeta, f"{nombre_base}_Composicion.tif")
                if not os.path.exists(comp_tif):
                    processing.run(
                        "gdal:merge",
                        {
                            'INPUT': tifs_to_merge,
                            'SEPARATE': True,
                            'NODATA_INPUT': -32768.0,
                            'NODATA_OUTPUT': -32768.0,
                            'EXTRA': '-init -32768',
                            'OUTPUT': comp_tif
                        },
                        context=context,
                        feedback=feedback
                    )

                    try:
                        dataset = gdal.Open(comp_tif, gdal.GA_Update)
                        if dataset:
                            for i, name in enumerate(order):
                                band = dataset.GetRasterBand(i + 1)
                                band.SetDescription(name)
                                band.SetNoDataValue(-32768)
                            dataset = None
                    except Exception as e:
                        feedback.pushInfo(
                            f"      ⚠ Error al asignar nombres a las bandas: {e}")

                    feedback.pushInfo(
                        self.tr("      ✔ Composición generada: {}").format(
                            os.path.basename(comp_tif)))
                else:
                    feedback.pushInfo(
                        self.tr("   ⏩ Ya existe composición: {}").format(
                            os.path.basename(comp_tif)))

                # Eliminar las bandas individuales para MCD43A4 para que el
                # dato de salida sea la composición
                for t in tifs_to_merge:
                    try:
                        if os.path.exists(t):
                            os.remove(t)
                    except BaseException:
                        pass

            feedback.pushInfo(
                self.tr("   ✅ {} banda(s) / composición exportada(s)").format(bandas_ok))
            total_exportados += bandas_ok

        feedback.pushInfo("")
        feedback.pushInfo(f"{'=' * 60}")
        feedback.pushInfo(self.tr(
            "✅ MODIS 43 — {} archivo(s) exportado(s) en total").format(total_exportados))
        return {self.OUTPUT_FOLDER: carpeta_out}

    def run(self):
        processing.execAlgorithmDialog(self)
