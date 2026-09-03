from .geomaticape_algorithm import GeomaticapeAlgorithm
# -*- coding: utf-8 -*-
"""
Factor de escala MODIS 12 - Cobertura del Suelo (MCD12Q1 v061)
Geomaticape Plugin - Conversion
Autor: GEOMATICA AMBIENTAL  |  Version: 1.3

Productos: MCD12Q1  →  500 m anual (Terra + Aqua)
           MCD12Q2  →  500 m anual (fenología)
           MCD12C1  →  0.05° anual (CMG)

Clasificaciones disponibles:
  LC_Type1 → IGBP Global Vegetation Classification (17 clases)
  LC_Type2 → UMD Land Cover Classification (15 clases)
  LC_Type3 → MODIS LAI/fPAR Classification (11 clases)
  LC_Type4 → MODIS BGC Classification (9 clases)
  LC_Type5 → Plant Functional Types Classification (11 clases)

Dato categórico — sin factor de escala numérico.
Remuestreo: vecino más cercano  |  Nodata: 255  |  WGS84 EPSG:4326
Por cada HDF se crea una subcarpeta. Se exporta tabla de clases CSV y QML.
Fuente: https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD12Q1
"""

import os
import csv

from qgis.core import (
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterEnum,
)
from qgis import processing


# ── Prefijos válidos ────────────────────────────────────────────────────────
PREFIJOS_MODIS12 = ["MCD12Q1", "MCD12Q2", "MCD12C1"]

# ── Definición de los 5 tipos de clasificación LC ──────────────────────────
# Cada entrada: (nombre_sds_en_hdf, sufijo_salida, nombre_legible)
LC_TYPES = [
    ("LC_Type1", "_LUC_Type1_IGBP", "IGBP Global Vegetation"),
    ("LC_Type2", "_LUC_Type2_UMD", "UMD Land Cover"),
    ("LC_Type3", "_LUC_Type3_LAI", "MODIS LAI/fPAR"),
    ("LC_Type4", "_LUC_Type4_BGC", "MODIS BGC"),
    ("LC_Type5", "_LUC_Type5_PFT", "Plant Functional Types"),
]

LC_TYPE_NAMES = [lc[0] for lc in LC_TYPES]   # para el parámetro Enum

# ── Tablas de clases (valor, R, G, B, descripción) ─────────────────────────
# Fuente: GEE Catalog MCD12Q1.061 + MODIS Land Cover User Guide v6.1
# Colores: paletas oficiales de GEE / LP DAAC

CLASES_LC_TYPE1 = [
    # val    R     G     B    descripción (GEE MCD12Q1.061)
    (1, 0x05, 0x45, 0x0a, "Bosques de hojas aciculares perennes"),
    (2, 0x08, 0x6a, 0x10, "Bosques perennifolios de hoja ancha"),
    (3, 0x54, 0xa7, 0x08, "Bosques de coníferas caducifolias"),
    (4, 0x78, 0xd2, 0x03, "Bosques caducifolios de hoja ancha"),
    (5, 0x00, 0x99, 0x00, "Bosques mixtos"),
    (6, 0xc6, 0xb0, 0x44, "Arbustales cerrados"),
    (7, 0xdc, 0xd1, 0x59, "Arbustales abiertos"),
    (8, 0xda, 0xde, 0x48, "Sabanas arboladas"),
    (9, 0xfb, 0xff, 0x13, "Sabanas"),
    (10, 0xb6, 0xff, 0x05, "Praderas"),
    (11, 0x27, 0xff, 0x87, "Humedales permanentes"),
    (12, 0xc2, 0x4f, 0x44, "Tierras de cultivo"),
    (13, 0xa5, 0xa5, 0xa5, "Terrenos urbanos y edificados"),
    (14, 0xff, 0x6d, 0x4c, "Mosaicos de tierras de cultivo y vegetación natural"),
    (15, 0x69, 0xff, 0xf8, "Nieve y hielo permanentes"),
    (16, 0xf9, 0xff, 0xa4, "Áreas descubiertas (arena, roca, suelo) con <10% vegetación"),
    (17, 0x1c, 0x0d, 0xff, "Cuerpos de agua"),
    (255, 0x00, 0x00, 0x00, "Sin datos (nodata)"),
]

CLASES_LC_TYPE2 = [
    (0, 0x1c, 0x0d, 0xff, "Cuerpos de agua"),
    (1, 0x05, 0x45, 0x0a, "Bosques de hojas aciculares perennes"),
    (2, 0x08, 0x6a, 0x10, "Bosques perennifolios de hoja ancha"),
    (3, 0x54, 0xa7, 0x08, "Bosques de coníferas caducifolias"),
    (4, 0x78, 0xd2, 0x03, "Bosques caducifolios de hoja ancha"),
    (5, 0x00, 0x99, 0x00, "Bosques mixtos"),
    (6, 0xc6, 0xb0, 0x44, "Arbustales cerrados"),
    (7, 0xdc, 0xd1, 0x59, "Arbustales abiertos"),
    (8, 0xda, 0xde, 0x48, "Sabanas arboladas"),
    (9, 0xfb, 0xff, 0x13, "Sabanas"),
    (10, 0xb6, 0xff, 0x05, "Praderas"),
    (11, 0x27, 0xff, 0x87, "Humedales permanentes"),
    (12, 0xc2, 0x4f, 0x44, "Tierras de cultivo"),
    (13, 0xa5, 0xa5, 0xa5, "Terrenos urbanos y edificados"),
    (14, 0xff, 0x6d, 0x4c, "Mosaicos de tierras de cultivo y vegetación natural"),
    (15, 0xf9, 0xff, 0xa4, "Tierras no vegetadas"),
    (255, 0x00, 0x00, 0x00, "Sin datos (nodata)"),
]

CLASES_LC_TYPE3 = [
    (0, 0x1c, 0x0d, 0xff, "Cuerpos de agua"),
    (1, 0xb6, 0xff, 0x05, "Praderas"),
    (2, 0xdc, 0xd1, 0x59, "Arbustales"),
    (3, 0xc2, 0x4f, 0x44, "Tierras de cultivo de hoja ancha"),
    (4, 0xfb, 0xff, 0x13, "Sabanas"),
    (5, 0x08, 0x6a, 0x10, "Bosques perennifolios de hoja ancha"),
    (6, 0x78, 0xd2, 0x03, "Bosques caducifolios de hoja ancha"),
    (7, 0x05, 0x45, 0x0a, "Bosques de hojas aciculares perennes"),
    (8, 0x54, 0xa7, 0x08, "Bosques de coníferas caducifolias"),
    (9, 0xf9, 0xff, 0xa4, "Tierras no vegetadas"),
    (10, 0xa5, 0xa5, 0xa5, "Terrenos urbanos y edificados"),
    (255, 0x00, 0x00, 0x00, "Sin datos (nodata)"),
]

CLASES_LC_TYPE4 = [
    (0, 0x1c, 0x0d, 0xff, "Cuerpos de agua"),
    (1, 0x05, 0x45, 0x0a, "Vegetación de hoja acicular perenne"),
    (2, 0x08, 0x6a, 0x10, "Vegetación de hoja ancha perenne"),
    (3, 0x54, 0xa7, 0x08, "Vegetación de hoja acicular caducifolia"),
    (4, 0x78, 0xd2, 0x03, "Vegetación de hoja ancha caducifolia"),
    (5, 0x00, 0x99, 0x00, "Vegetación anual de hoja ancha"),
    (6, 0xb6, 0xff, 0x05, "Vegetación de pastos anuales"),
    (7, 0xf9, 0xff, 0xa4, "Tierras sin vegetación"),
    (8, 0xa5, 0xa5, 0xa5, "Terrenos urbanos y edificados"),
    (255, 0x00, 0x00, 0x00, "Sin datos (nodata)"),
]

CLASES_LC_TYPE5 = [
    (0, 0x1c, 0x0d, 0xff, "Cuerpos de agua"),
    (1, 0x05, 0x45, 0x0a, "Árboles de hoja acicular perenne"),
    (2, 0x08, 0x6a, 0x10, "Árboles de hoja perenne y hoja ancha"),
    (3, 0x54, 0xa7, 0x08, "Árboles de hoja caduca aciculares"),
    (4, 0x78, 0xd2, 0x03, "Árboles de hoja ancha caducifolios"),
    (5, 0xdc, 0xd1, 0x59, "Arbustos"),
    (6, 0xb6, 0xff, 0x05, "Herbáceos no cultivados"),
    (7, 0xda, 0xde, 0x48, "Tierras de cultivo de cereales"),
    (8, 0xc2, 0x4f, 0x44, "Tierras de cultivo de hoja ancha"),
    (9, 0xa5, 0xa5, 0xa5, "Terrenos urbanos y edificados"),
    (10, 0x69, 0xff, 0xf8, "Nieve y hielo permanentes"),
    (11, 0xf9, 0xff, 0xa4, "Tierras sin vegetación"),
    (255, 0x00, 0x00, 0x00, "Sin datos (nodata)"),
]

# Mapeo índice → tabla de clases
TABLAS_CLASES = {
    0: CLASES_LC_TYPE1,
    1: CLASES_LC_TYPE2,
    2: CLASES_LC_TYPE3,
    3: CLASES_LC_TYPE4,
    4: CLASES_LC_TYPE5,
}

FACTOR = 1.0
OFFSET = 0.0
NODATA_OUT = 255
DTYPE_OUT = "uint8"
RESAMPLE_NN = True   # obligatorio para dato categórico


# ── Función: exportar tabla de clases CSV ───────────────────────────────────
def exportar_tabla_csv(ruta_csv, clases, lc_nombre):
    """
    Genera un archivo CSV con: Valor, R, G, B, Color_Hex, Descripción.
    Listo para ser consultado como tabla de simbología.
    """
    with open(ruta_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Valor", "R", "G", "B", "Color_Hex",
                        "Descripcion", "LC_Type"])
        for val, r, g, b, desc in clases:
            hex_color = f"#{r:02X}{g:02X}{b:02X}"
            writer.writerow([val, r, g, b, hex_color, desc, lc_nombre])


# ── Función: exportar QML (simbología QGIS) ────────────────────────────────
def exportar_qml(ruta_qml, clases, titulo):
    """
    Genera un archivo QML de simbología de valores únicos (paletted)
    compatible con QGIS 3.x para aplicar directamente al raster.

    IMPORTANTE: QGIS 3.x requiere <paletteEntry> (NO <item>) dentro
    de <colorPalette> en el renderer 'paletted'.
    """
    entradas = []
    for val, r, g, b, desc in clases:
        if val == 255:
            continue   # el nodata no se incluye en la leyenda
        entradas.append(
            f'        <paletteEntry value="{val}" '
            f'color="#{r:02x}{g:02x}{b:02x}" '
            f'label="{desc}" alpha="255"/>'
        )

    entradas_xml = "\n".join(entradas)

    qml = f"""<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.16" styleCategories="AllStyleCategories">
  <pipe>
    <provider>
      <resampling enabled="false" zoomedInResamplingMethod="nearestNeighbour"
                  zoomedOutResamplingMethod="nearestNeighbour" maxOversampling="2"/>
    </provider>
    <rasterrenderer type="paletted" band="1" opacity="1" nodataColor="">
      <rasterTransparency/>
      <minMaxOrigin>
        <limits>None</limits>
        <extent>WholeRaster</extent>
        <statAccuracy>Estimated</statAccuracy>
        <cumulativeCutLower>0.02</cumulativeCutLower>
        <cumulativeCutUpper>0.98</cumulativeCutUpper>
        <stdDevFactor>2</stdDevFactor>
      </minMaxOrigin>
      <colorPalette>
{entradas_xml}
      </colorPalette>
      <colorRamp type="randomcolors" name="[source]"/>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation colorizeOn="0" colorizeRed="255" colorizeGreen="128"
                   colorizeBlue="128" colorizeStrength="100"
                   grayscaleMode="0" saturation="0"/>
    <rasterresampler maxOversampling="2"/>
    <resamplingStage>resamplingFilter</resamplingStage>
  </pipe>
  <layertreelayer>
    <customproperties>
      <property key="identify/format" value="Class"/>
    </customproperties>
  </layertreelayer>
  <legend type="default-vector"/>
</qgis>
"""
    with open(ruta_qml, "w", encoding="utf-8") as f:
        f.write(qml)


# ── Función: detectar prefijo ───────────────────────────────────────────────
def _detectar_prefijo(nombre_hdf):
    nombre_upper = nombre_hdf.upper()
    for p in PREFIJOS_MODIS12:
        if nombre_upper.startswith(p):
            return p
    return None


def _buscar_sds(sds_lista, nombre_banda):
    nombre_lower = nombre_banda.lower()
    for sds_path, sds_desc in sds_lista:
        if nombre_lower in sds_path.lower() or nombre_lower in sds_desc.lower():
            return sds_path
    return None


# ── Algoritmo principal ─────────────────────────────────────────────────────
class FactorMODIS12(GeomaticapeAlgorithm):
    _algorithm_name = "factor_modis12"
    _icon_name = "clasificacion.png"

    INPUT_FOLDER = "INPUT_FOLDER"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    LC_TYPE_SEL = "LC_TYPE_SEL"

    def displayName(self):
        return self.tr("Factor escala MODIS 12 (Cobertura del Suelo)")

    def group(self):
        return self.tr("Conversion")

    def groupId(self): return "geomaticape_conversion"

    def shortHelpString(self):
        return """
<h3>MODIS 12 — Cobertura del Suelo (MCD12Q1 v061)</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape &nbsp;|&nbsp; <b>Versión:</b> 1.3<br><br>
<b>Productos:</b>
<ul>
<li>MCD12Q1 → 500 m anual (Terra + Aqua)</li>
<li>MCD12Q2 → 500 m anual (fenología)</li>
<li>MCD12C1 → 0.05° anual (CMG Global)</li>
</ul>
<b>Clasificaciones LC_Type disponibles:</b>
<ul>
<li><b>LC_Type1</b> — IGBP Global Vegetation (17 clases)</li>
<li><b>LC_Type2</b> — UMD Land Cover (14 clases)</li>
<li><b>LC_Type3</b> — MODIS LAI/fPAR (11 clases)</li>
<li><b>LC_Type4</b> — MODIS BGC (8 clases)</li>
<li><b>LC_Type5</b> — Plant Functional Types (10 clases)</li>
</ul>
<b>Organización:</b> Por cada HDF se crea una subcarpeta con el nombre base del producto.<br>
<b>Salidas por HDF:</b>
<ul>
<li>GeoTIFF de la clasificación seleccionada</li>
<li>CSV con tabla de clases (Valor, R, G, B, Color_Hex, Descripción)</li>
<li>QML con simbología lista para QGIS</li>
</ul>
<b>Dato categórico</b> — remuestreo vecino más cercano.<br>
<b>Nodata:</b> 255 &nbsp;|&nbsp; <b>Proyección:</b> WGS84 EPSG:4326<br>
<b>Fuente:</b> <a href="https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD12Q1">GEE MCD12Q1.061</a><br>
<b>Web:</b> https://www.geomatica.pe/
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_FOLDER,
            self.tr("Carpeta con archivos MODIS 12 HDF\n(MCD12Q1, MCD12Q2, MCD12C1)"),
            behavior=QgsProcessingParameterFile.Behavior.Folder
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.LC_TYPE_SEL,
            self.tr("Clasificación LC_Type a exportar"),
            options=[
                "LC_Type1 — IGBP Global Vegetation (17 clases)",
                "LC_Type2 — UMD Land Cover (14 clases)",
                "LC_Type3 — MODIS LAI/fPAR (11 clases)",
                "LC_Type4 — MODIS BGC (8 clases)",
                "LC_Type5 — Plant Functional Types (10 clases)",
            ],
            defaultValue=0,
            optional=False,
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER,
            self.tr("Carpeta raíz de salida (se crean subcarpetas por producto)")
        ))

    # --------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):

        from ._modis_core import listar_sds, procesar_banda

        carpeta_in = self.parameterAsString(
            parameters, self.INPUT_FOLDER, context)
        carpeta_out = self.parameterAsString(
            parameters, self.OUTPUT_FOLDER, context)
        lc_idx = self.parameterAsEnum(parameters, self.LC_TYPE_SEL, context)
        os.makedirs(carpeta_out, exist_ok=True)

        # ── Resolución parámetros LC seleccionado ───────────────────────────
        lc_sds, lc_sufijo, lc_nombre = LC_TYPES[lc_idx]
        clases_tabla = TABLAS_CLASES[lc_idx]

        feedback.pushInfo(
            f"🗺  Clasificación seleccionada : {lc_sds} — {lc_nombre}")
        feedback.pushInfo(
            f"📋 Número de clases           : {
                len(clases_tabla) -
                1} (+nodata)")
        feedback.pushInfo("")

        # ── Recopilar HDF válidos ────────────────────────────────────────────
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
                "No se detectó ningún producto MODIS 12 válido.\n"
                "Prefijos esperados: " + ", ".join(PREFIJOS_MODIS12)
            )

        feedback.pushInfo(
            f"📄 Archivos MODIS 12 encontrados: {
                len(hdfs_validos)}")
        feedback.pushInfo("")

        total_exportados = 0
        resultados = []

        for idx, (hdf, prefijo) in enumerate(hdfs_validos):
            if feedback.isCanceled():
                break

            ruta_hdf = os.path.join(carpeta_in, hdf)
            nombre_base = os.path.splitext(hdf)[0]   # nombre completo sin .hdf

            # ── Crear subcarpeta ────────────────────────────────────────────
            subcarpeta = os.path.join(carpeta_out, nombre_base)
            os.makedirs(subcarpeta, exist_ok=True)

            feedback.pushInfo(f"{'─' * 60}")
            feedback.pushInfo(f"📄 ({idx + 1}/{len(hdfs_validos)}) {hdf}")
            feedback.pushInfo(f"   🗺  Producto   : {prefijo}")
            feedback.pushInfo(f"   🏷  LC_Type    : {lc_sds} ({lc_nombre})")
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

            # ── Buscar SDS del LC_Type seleccionado ─────────────────────────
            sds_path = _buscar_sds(sds_lista, lc_sds)
            if sds_path is None:
                feedback.reportError(
                    f"   ⚠ '{lc_sds}' no encontrado en los subdatasets de {hdf}", False
                )
                continue

            nombre_tif = nombre_base + lc_sufijo + ".tif"
            nombre_csv = nombre_base + lc_sufijo + "_clases.csv"
            nombre_qml = nombre_base + lc_sufijo + ".qml"

            ruta_tif = os.path.join(subcarpeta, nombre_tif)
            ruta_csv = os.path.join(subcarpeta, nombre_csv)
            ruta_qml = os.path.join(subcarpeta, nombre_qml)

            # ── Exportar GeoTIFF ────────────────────────────────────────────
            if os.path.exists(ruta_tif):
                feedback.pushInfo(f"   ⏩ Ya existe GeoTIFF: {nombre_tif}")
                resultados.append(ruta_tif)
                total_exportados += 1
            else:
                feedback.pushInfo(
                    f"   ➤ Exportando: {lc_sds}  →  {nombre_tif}")
                ok = procesar_banda(
                    sds_path=sds_path,
                    ruta_tif_out=ruta_tif,
                    factor=FACTOR,
                    offset=OFFSET,
                    nodata_out=NODATA_OUT,
                    dtype_out=DTYPE_OUT,
                    resample_nn=RESAMPLE_NN,
                    feedback=feedback,
                )
                if ok:
                    feedback.pushInfo(f"      ✔ GeoTIFF: {nombre_tif}")
                    resultados.append(ruta_tif)
                    total_exportados += 1
                else:
                    feedback.reportError(
                        f"      ✘ Fallo al exportar {nombre_tif}", False)
                    continue

            # ── Exportar tabla CSV de clases ────────────────────────────────
            try:
                exportar_tabla_csv(ruta_csv, clases_tabla, lc_sds)
                feedback.pushInfo(f"      ✔ CSV clases: {nombre_csv}")
            except Exception as e:
                feedback.reportError(
                    f"      ⚠ Error exportando CSV: {e}", False)

            # ── Exportar simbología QML ─────────────────────────────────────
            try:
                exportar_qml(ruta_qml, clases_tabla, f"{lc_sds} — {lc_nombre}")
                feedback.pushInfo(f"      ✔ QML simbología: {nombre_qml}")
            except Exception as e:
                feedback.reportError(
                    f"      ⚠ Error exportando QML: {e}", False)

        # ── Resumen final ────────────────────────────────────────────────────
        feedback.pushInfo("")
        feedback.pushInfo(f"{'=' * 60}")
        feedback.pushInfo(
            f"✅ MODIS 12 — {total_exportados} GeoTIFF exportado(s)")
        feedback.pushInfo(f"   Clasificación : {lc_sds} — {lc_nombre}")
        feedback.pushInfo(f"   Carpeta raíz  : {carpeta_out}")
        feedback.pushInfo("   📌 Archivos generados por producto:")
        feedback.pushInfo(f"      · GeoTIFF  (*{lc_sufijo}.tif)")
        feedback.pushInfo(f"      · CSV      (*{lc_sufijo}_clases.csv)")
        feedback.pushInfo(f"      · QML      (*{lc_sufijo}.qml)")
        return {self.OUTPUT_FOLDER: carpeta_out}

    def run(self):
        processing.execAlgorithmDialog(self)
