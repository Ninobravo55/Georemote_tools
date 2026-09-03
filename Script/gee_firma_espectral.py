from ._qt_compat import qt_exec
from .geomaticape_algorithm import GeomaticapeAlgorithm
# -*- coding: utf-8 -*-
"""
gee_firma_espectral.py
Obtiene firmas espectrales profesionales en la nube mediante Google Earth Engine
para Landsat 4, 5, 7, 8, 9 y Sentinel-2.

Logica interna:
  - Lee la capa de muestreo (puntos O poligonos) de QGIS y la reproyecta a WGS84.
  - Serializa cada geometria a GeoJSON para su envio nativo a GEE.
  - Busca la mejor escena en GEE (menor nubosidad dentro del periodo, AOI y cobertura).
  - Enmascara nubes y sombras en la escena elegida.
  - Extrae en la nube los valores de reflectancia de todas las bandas multiespectrales.
    · Puntos  → ee.Reducer.mean() calcula el valor del pixel exacto.
    · Poligonos → ee.Reducer.mean() promedia todos los pixeles dentro del area.
  - Escala la reflectancia al rango [0, 1] y agrupa por clase (min, max, promedio).
  - Genera un archivo Excel (.xlsx) estructurado con Pandas.
  - Genera un grafico profesional (.png) de las firmas espectrales usando Matplotlib.
  - Exporta opcionalmente la escena a Google Drive si se requiere.

GeomaticaPe — Geomatica Ambiental
"""

import os
import re
import datetime
import json

from qgis.core import (
    QgsProcessingException,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFileDestination,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsWkbTypes,
    QgsProcessing,
    QgsProcessingParameterFolderDestination,
)

import math

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────
GDRIVE_LANDSAT = 'Pedido_Geomatica_Landsat'
GDRIVE_SENTINEL = 'Pedido_Geomatica_Sentinel2'
SCALE_COVERAGE = 300   # resolucion para calculo de cobertura (m)
LIMITE_DIRECTO_MP = 10_000_000  # pixeles — umbral para descarga directa

# Paleta de colores para clases (se repite ciclicamente)
COLORES = [
    '#2ecc71',  # verde
    '#3498db',  # azul
    '#e74c3c',  # rojo
    '#9b59b6',  # morado
    '#f39c12',  # naranja
    '#1abc9c',  # turquesa
    '#e67e22',  # naranja oscuro
    '#34495e',  # gris oscuro
    '#c0392b',  # rojo oscuro
    '#16a085',  # verde mar
]

# ─────────────────────────────────────────────────────────────────────────────
# Catalogo de sensores
# ─────────────────────────────────────────────────────────────────────────────
SENSORES = {
    'Landsat 4 C2 L2 (SR)': {
        'collection': 'LANDSAT/LT04/C02/T1_L2',
        'sensor': 'LANDSAT_4',
        'tipo': 'landsat',
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'bandas': ['blue', 'green', 'red', 'nir', 'swir1', 'swir2'],
        'bandas_gee': ['SR_B1', 'SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B7'],
        'longitud': [0.490, 0.560, 0.660, 0.830, 1.650, 2.220],
        'titulo': 'Firma Espectral GEE — Landsat 4 TM',
        'gdrive': GDRIVE_LANDSAT,
    },
    'Landsat 5 C2 L2 (SR)': {
        'collection': 'LANDSAT/LT05/C02/T1_L2',
        'sensor': 'LANDSAT_5',
        'tipo': 'landsat',
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'bandas': ['blue', 'green', 'red', 'nir', 'swir1', 'swir2'],
        'bandas_gee': ['SR_B1', 'SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B7'],
        'longitud': [0.490, 0.560, 0.660, 0.830, 1.650, 2.220],
        'titulo': 'Firma Espectral GEE — Landsat 5 TM',
        'gdrive': GDRIVE_LANDSAT,
    },
    'Landsat 7 C2 L2 (SR)': {
        'collection': 'LANDSAT/LE07/C02/T1_L2',
        'sensor': 'LANDSAT_7',
        'tipo': 'landsat',
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'bandas': ['blue', 'green', 'red', 'nir', 'swir1', 'swir2'],
        'bandas_gee': ['SR_B1', 'SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B7'],
        'longitud': [0.485, 0.560, 0.660, 0.830, 1.650, 2.220],
        'titulo': 'Firma Espectral GEE — Landsat 7 ETM+',
        'gdrive': GDRIVE_LANDSAT,
    },
    'Landsat 8 C2 L2 (SR)': {
        'collection': 'LANDSAT/LC08/C02/T1_L2',
        'sensor': 'LANDSAT_8',
        'tipo': 'landsat',
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'bandas': ['aerosol', 'blue', 'green', 'red', 'nir', 'swir1', 'swir2'],
        'bandas_gee': ['SR_B1', 'SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B6', 'SR_B7'],
        'longitud': [0.440, 0.480, 0.560, 0.660, 0.865, 1.610, 2.200],
        'titulo': 'Firma Espectral GEE — Landsat 8 OLI',
        'gdrive': GDRIVE_LANDSAT,
    },
    'Landsat 9 C2 L2 (SR)': {
        'collection': 'LANDSAT/LC09/C02/T1_L2',
        'sensor': 'LANDSAT_9',
        'tipo': 'landsat',
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'bandas': ['aerosol', 'blue', 'green', 'red', 'nir', 'swir1', 'swir2'],
        'bandas_gee': ['SR_B1', 'SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B6', 'SR_B7'],
        'longitud': [0.440, 0.480, 0.560, 0.660, 0.865, 1.610, 2.200],
        'titulo': 'Firma Espectral GEE — Landsat 9 OLI-2',
        'gdrive': GDRIVE_LANDSAT,
    },
    'Sentinel-2 C2L2 (SR Harmonized)': {
        'collection': 'COPERNICUS/S2_SR_HARMONIZED',
        'sensor': 'SENTINEL2_L2A',
        'tipo': 'sentinel2',
        'cloud_prop': 'CLOUDY_PIXEL_PERCENTAGE',
        'scale_sr': 10,
        'bandas': ['aerosol', 'blue', 'green', 'red', 'red_edge1', 'red_edge2', 'red_edge3', 'nir', 'swir1', 'swir2'],
        'bandas_gee': ['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B11', 'B12'],
        'longitud': [0.4427, 0.4924, 0.5598, 0.6646, 0.7041, 0.7405, 0.7828, 0.8328, 1.6137, 2.2024],
        'titulo': 'Firma Espectral GEE — Sentinel-2 MSI (L2A)',
        'gdrive': GDRIVE_SENTINEL,
    },
    'Sentinel-2 C2L1 (TOA Harmonized)': {
        'collection': 'COPERNICUS/S2_HARMONIZED',
        'sensor': 'SENTINEL2_L1C',
        'tipo': 'sentinel2',
        'cloud_prop': 'CLOUDY_PIXEL_PERCENTAGE',
        'scale_sr': 10,
        'bandas': ['aerosol', 'blue', 'green', 'red', 'red_edge1', 'red_edge2', 'red_edge3', 'nir', 'swir1', 'swir2'],
        'bandas_gee': ['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B11', 'B12'],
        'longitud': [0.4427, 0.4924, 0.5598, 0.6646, 0.7041, 0.7405, 0.7828, 0.8328, 1.6137, 2.2024],
        'titulo': 'Firma Espectral GEE — Sentinel-2 MSI (L1C)',
        'gdrive': GDRIVE_SENTINEL,
    },
}

SENSOR_LABELS = list(SENSORES.keys())

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _ensure_ee(feedback):
    try:
        import ee
        return ee
    except ImportError:
        raise QgsProcessingException(
            "Falta el modulo 'earthengine-api'.\n"
            "Instalalo en la OSGeo4W Shell (Windows):\n"
            "   python -m pip install earthengine-api\n"
            "Linux / macOS:\n"
            "   pip install earthengine-api"
        )


def _autenticar_gee(ee, feedback):
    from qgis.core import QgsSettings
    try:
        settings = QgsSettings()
        proyecto = settings.value(
            'geomaticape/gee_project', '', type=str).strip()
        kwargs = {}
        if proyecto:
            kwargs['project'] = proyecto

        feedback.pushInfo(
            "Conectando GEE con credenciales locales guardadas...")
        if proyecto:
            feedback.pushInfo(f"Proyecto GEE: {proyecto}")

        ee.Initialize(**kwargs)
        feedback.pushInfo("Conexion a Google Earth Engine exitosa.")

    except Exception as ex:
        msg = str(ex)
        if any(k in msg.lower()
               for k in ('authorize', 'credentials', 'oauth', 'token', 'project')):
            raise QgsProcessingException(
                "No se encontraron credenciales GEE validas.\n\n"
                "Ve al menu: Geomaticape Tools -> Descarga GEE -> Autenticar Google Earth Engine.\n"
            )
        raise QgsProcessingException(f"Error al conectar GEE: {msg}")


def _limpiar_nombre(texto):
    """Sanitiza el nombre del archivo para GEE (max 100 chars)."""
    texto = re.sub(r'[^a-zA-Z0-9._-]', '_', str(texto))
    return texto[:100]


def _mask_landsat(image):
    """
    Enmascara nubes y sombras en Landsat C2 L2 (todos los sensores).
    QA_PIXEL: bit 3 = nube, bit 4 = sombra de nube.
    """
    qa = image.select('QA_PIXEL')
    mask = (
        qa.bitwiseAnd(1 << 3).eq(0)       # sin nube
        .And(qa.bitwiseAnd(1 << 4).eq(0))  # sin sombra
    )
    return image.updateMask(mask)


def _mask_sentinel2(image):
    """
    Enmascara nubes y cirros en Sentinel-2.
    QA60: bit 10 = nube opaca, bit 11 = cirro.
    """
    qa = image.select('QA60')
    mask = (
        qa.bitwiseAnd(1 << 10).eq(0)
        .And(qa.bitwiseAnd(1 << 11).eq(0))
    )
    return image.updateMask(mask)


def _filtro_geometrico(collection, aoi):
    """Nivel 1: footprint de la imagen debe contener completamente el AOI."""
    return collection.filter(
        ee.Filter.contains(leftField='.geo', rightValue=aoi)
    )


def _calcular_cobertura(ee, image, aoi, scale=SCALE_COVERAGE):
    """
    Calcula la fraccion de pixeles validos (no enmascarados) dentro del AOI.
    Devuelve la imagen con la propiedad 'aoi_coverage' [0-1].
    """
    total_px = (
        ee.Image.constant(1)
        .reduceRegion(
            reducer=ee.Reducer.count(),
            geometry=aoi,
            scale=scale,
            maxPixels=1e9
        )
        .values().get(0)
    )
    valid_px = (
        image.select(0).mask()
        .reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=aoi,
            scale=scale,
            maxPixels=1e9
        )
        .values().get(0)
    )
    cobertura = ee.Number(valid_px).divide(ee.Number(total_px))
    return image.set({'aoi_coverage': cobertura})


def _aplicar_filtros(ee, collection, aoi, min_coverage, feedback):
    """
    Aplica filtro geometrico + filtro de cobertura real de pixeles.
    Devuelve la coleccion ordenada por nubosidad de forma ascendente.
    """
    col_l1 = _filtro_geometrico(collection, aoi)
    col_l2 = col_l1.map(lambda img: _calcular_cobertura(ee, img, aoi))
    col_l2 = col_l2.filter(ee.Filter.gte('aoi_coverage', min_coverage))
    return col_l2


def _estimar_pixeles(lon_min, lat_min, lon_max, lat_max, scale):
    """Pixeles con correccion coseno latitudinal."""
    lat_med = (lat_min + lat_max) / 2.0
    cos_lat = math.cos(math.radians(lat_med))
    ancho_m = (lon_max - lon_min) * 111320.0 * cos_lat
    alto_m = (lat_max - lat_min) * 111320.0
    px_x = max(1, int(ancho_m / scale))
    px_y = max(1, int(alto_m / scale))
    return px_x * px_y


def _descarga_directa(ee, imagen, scale, region, output_path, feedback):
    """getDownloadURL -> GeoTIFF local. Maneja ZIP o TIF directo y restaura nombres de bandas."""
    import zipfile
    import os
    import shutil
    from urllib.parse import urlparse

    feedback.pushInfo(
        f"Generando URL de descarga directa en GEE para {
            os.path.basename(output_path)}...")

    # Obtener nombres de bandas para restaurarlos luego
    try:
        band_names = imagen.bandNames().getInfo()
    except BaseException:
        band_names = []

    try:
        url = imagen.getDownloadURL({
            'scale': scale,
            'region': region,
            'format': 'GEO_TIFF',
            'crs': 'EPSG:4326',
        })
    except Exception as ex:
        msg = str(ex)
        if '50331648' in msg or 'request size' in msg.lower(
        ) or 'must be less than' in msg.lower():
            raise QgsProcessingException(
                "GEE rechaza la descarga directa porque el archivo supera 48 MB.\n"
                "Reduce el área, aumenta la escala, o selecciona Drive/GCS.\n\n"
                f"Detalle GEE: {msg}"
            )
        raise QgsProcessingException(f"Error al generar URL: {msg}")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise QgsProcessingException(
            f"Esquema de URL no permitido: {parsed.scheme}"
        )

    feedback.pushInfo("Descargando GeoTIFF desde GEE...")
    tmp = output_path + '_tmp.bin'

    try:
        from qgis.core import QgsNetworkAccessManager
        from qgis.PyQt.QtNetwork import QNetworkRequest
        from qgis.PyQt.QtCore import QUrl, QEventLoop, QFile, QIODevice, QTimer

        manager = QgsNetworkAccessManager.instance()
        req = QNetworkRequest(QUrl(url))
        reply = manager.get(req)

        loop = QEventLoop()
        reply.finished.connect(loop.quit)

        timer = QTimer()
        timer.setInterval(500)

        out_file = QFile(tmp)
        if not out_file.open(QIODevice.OpenModeFlag.WriteOnly):
            reply.abort()
            raise QgsProcessingException(
                "No se pudo abrir el archivo temporal para descarga.")

        def on_ready_read():
            out_file.write(reply.readAll())

        def check_cancel():
            if feedback.isCanceled():
                reply.abort()

        reply.readyRead.connect(on_ready_read)
        timer.timeout.connect(check_cancel)
        timer.start()

        qt_exec(loop)
        timer.stop()
        out_file.close()

        if reply.error() != 0:
            if feedback.isCanceled():
                raise QgsProcessingException("Cancelado por el usuario.")
            raise QgsProcessingException(
                f"Error de red/HTTP: {reply.errorString()}")

    except Exception as ex:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except BaseException:
                pass
        raise QgsProcessingException(f"Error en descarga: {str(ex)}")

    if zipfile.is_zipfile(tmp):
        feedback.pushInfo("Descomprimiendo ZIP de GEE...")
        with zipfile.ZipFile(tmp, 'r') as zf:
            tifs = [n for n in zf.namelist() if n.lower().endswith('.tif')]
            if not tifs:
                raise QgsProcessingException(
                    "El ZIP no contiene archivos .tif")

            extracted = zf.extract(
                tifs[0], os.path.dirname(output_path) or '.')
        shutil.move(extracted, output_path)
        os.remove(tmp)
    else:
        shutil.move(tmp, output_path)

    # Restaurar nombres de bandas internos usando GDAL
    if band_names:
        try:
            from osgeo import gdal
            ds = gdal.Open(output_path, gdal.GA_Update)
            if ds:
                for i, bname in enumerate(band_names):
                    if i < ds.RasterCount:
                        band = ds.GetRasterBand(i + 1)
                        band.SetDescription(bname)
                ds = None
        except Exception as e:
            feedback.pushWarning(
                f"No se pudieron renombrar las bandas internas: {e}")

    feedback.pushInfo(f"Guardado en: {output_path}")
    return output_path


def _exportar_imagen(ee, imagen, nombre, destino,
                     escala, aoi, metodo, feedback):
    """Inicia una tarea Export.image.toDrive o toCloudStorage para una imagen."""
    if metodo == 1:  # GCS
        task = ee.batch.Export.image.toCloudStorage(
            image=imagen,
            description=nombre,
            bucket=destino,
            fileNamePrefix=nombre,
            region=aoi,
            scale=escala,
            crs='EPSG:4326',
            fileFormat='GeoTIFF',
            maxPixels=1e13,
        )
    else:  # Drive
        task = ee.batch.Export.image.toDrive(
            image=imagen,
            description=nombre,
            folder=destino,
            fileNamePrefix=nombre,
            region=aoi,
            scale=escala,
            crs='EPSG:4326',
            fileFormat='GeoTIFF',
            maxPixels=1e13,
        )
    task.start()
    return task.id


# ─────────────────────────────────────────────────────────────────────────────
# Algoritmo QGIS Processing
# ─────────────────────────────────────────────────────────────────────────────

class GEEFirmaEspectral(GeomaticapeAlgorithm):
    _algorithm_name = "gee_firma_espectral"
    _icon_name = "default.png"

    EXTENT = 'EXTENT'
    PUNTOS = 'PUNTOS'
    CAMPO = 'CAMPO'
    SENSOR = 'SENSOR'
    DATE_START = 'DATE_START'
    DATE_END = 'DATE_END'
    CLOUD_PCT = 'CLOUD_PCT'
    MIN_COVERAGE = 'MIN_COVERAGE'
    EXPORT_DRIVE = 'EXPORT_DRIVE'
    EXPORT_METHOD = 'EXPORT_METHOD'
    GCS_BUCKET = 'GCS_BUCKET'
    LOCAL_DIR = 'LOCAL_DIR'
    EXCEL = 'EXCEL'
    GRAFICO = 'GRAFICO'

    def displayName(self):
        return self.tr('Firma espectral profesional (GEE)')

    def group(self):
        return self.tr('Descarga GEE')

    def groupId(self):
        return 'descarga_gee'

    def tags(self):
        return [
            'gee', 'firma espectral', 'landsat', 'sentinel', 'satelite',
            'reflectancia', 'excel', 'grafico', 'cobertura', 'muestreo'
        ]

    def shortHelpString(self):
        return """
<b>Firma espectral profesional en la nube mediante Google Earth Engine</b><br><br>

Busca la escena satelital mas limpia (menor nubosidad) en el periodo e interest geográfico,
enmascara nubes y sombras, y extrae la firma espectral de todas las bandas multiespectrales
en objetos de muestreo (<b>puntos o polígonos</b>) directamente en los servidores de GEE.<br><br>

<b>Tipos de capa de muestreo aceptados:</b><br>
<ul>
  <li><b>Puntos</b> → extrae el valor exacto del pixel bajo cada punto.</li>
  <li><b>Polígonos</b> → extrae el promedio de todos los pixels contenidos dentro de cada polígono,
  obteniendo firmas espectrales más representativas y estables para cada cobertura.</li>
</ul>

<b>Integración y Exportación:</b><br>
Los resultados incluyen estadísticas agrupadas y un reporte listo para análisis.<br><br>

<b>Entregables:</b><br>
- <b>Excel (.xlsx)</b>: Hoja con datos crudos escalados y hoja resumen por clase (promedio, mínimo, máximo de reflectancia).<br>
- <b>Gráfico (.png)</b>: Curva de firma espectral promedio con bandas de rango (mínimo - máximo) coloreadas por clase.<br><br>

<b>Nota de Reflectancias:</b> Las reflectancias son normalizadas de forma automática al rango <code>[0.0 - 1.0]</code>.
"""

    def initAlgorithm(self, config=None):

        # ── Extension ────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT,
            self.tr('Extension del area de interes (AOI)')
        ))

        # ── Capa de Muestreo (Puntos o Poligonos) ───────────────
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.PUNTOS,
            self.tr('Capa de muestreo (Puntos o Poligonos)'),
            types=[
                QgsProcessing.SourceType.TypeVectorPoint,
                QgsProcessing.SourceType.TypeVectorPolygon,
            ]
        ))
        self.addParameter(QgsProcessingParameterField(
            self.CAMPO,
            self.tr('Campo de clase / cobertura (texto)'),
            parentLayerParameterName=self.PUNTOS,
            type=QgsProcessingParameterField.DataType.String,
            defaultValue='Clase'
        ))

        # ── Sensor ───────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterEnum(
            self.SENSOR,
            self.tr('Sensor / Coleccion satelital'),
            options=SENSOR_LABELS,
            defaultValue=3   # Landsat 8 por defecto
        ))

        # ── Fechas ───────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterString(
            self.DATE_START,
            self.tr('Fecha inicio (YYYY-MM-DD)'),
            defaultValue='2020-01-01'
        ))
        self.addParameter(QgsProcessingParameterString(
            self.DATE_END,
            self.tr('Fecha fin (YYYY-MM-DD)'),
            defaultValue=datetime.date.today().strftime('%Y-%m-%d')
        ))

        # ── Nubosidad ────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterNumber(
            self.CLOUD_PCT,
            self.tr('Nubosidad maxima (%)'),
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=30.0,
            minValue=0.0,
            maxValue=100.0
        ))

        # ── Cobertura minima ─────────────────────────────────────
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_COVERAGE,
            self.tr(
                'Cobertura minima de pixeles validos en el AOI [0.0 - 1.0]'),
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=0.80,
            minValue=0.0,
            maxValue=1.0,
            optional=True
        ))

        # ── Exportar Escena ─────────────────────────────────────
        self.addParameter(QgsProcessingParameterBoolean(
            self.EXPORT_DRIVE,
            self.tr('Exportar escena multiespectral elegida'),
            defaultValue=False
        ))

        self.addParameter(QgsProcessingParameterEnum(
            self.EXPORT_METHOD,
            self.tr('Método de exportación (solo si exporta)'),
            options=[
                'Google Drive',
                'Google Cloud Storage (GCS)',
                'Descarga Directa (imágenes pequeñas)'],
            defaultValue=0
        ))
        self.addParameter(QgsProcessingParameterString(
            self.GCS_BUCKET,
            self.tr('Bucket de Google Cloud Storage (solo GCS)'),
            defaultValue='',
            optional=True
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.LOCAL_DIR,
            self.tr('Carpeta local de destino (solo Descarga Directa)'),
            optional=True
        ))

        # ── Salidas ──────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterFileDestination(
            self.EXCEL,
            self.tr('Archivo Excel de salida (.xlsx)'),
            fileFilter='Excel (*.xlsx)'
        ))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.GRAFICO,
            self.tr('Grafico PNG de salida (.png)'),
            fileFilter='PNG (*.png)'
        ))

    # ─────────────────────────────────────────────────────────────
    def processAlgorithm(self, parameters, context, feedback):
        # Importaciones diferidas
        try:
            import pandas as pd
            import numpy as np
        except ImportError:
            raise QgsProcessingException(
                "Faltan bibliotecas requeridas. Instala pandas y openpyxl:\n"
                "python -m pip install pandas openpyxl"
            )
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.patches as mpatches
        except ImportError:
            raise QgsProcessingException(
                "Falta la biblioteca matplotlib para el grafico.\n"
                "python -m pip install matplotlib"
            )

        # ── Leer parametros ───────────────────────────────────────
        idx_sensor = self.parameterAsEnum(parameters, self.SENSOR, context)
        puntos_layer = self.parameterAsVectorLayer(
            parameters, self.PUNTOS, context)
        campo_clase = self.parameterAsString(parameters, self.CAMPO, context)
        fecha_inicio = self.parameterAsString(
            parameters, self.DATE_START, context).strip()
        fecha_fin = self.parameterAsString(
            parameters, self.DATE_END, context).strip()
        cloud_pct = self.parameterAsDouble(parameters, self.CLOUD_PCT, context)
        min_cov = self.parameterAsDouble(
            parameters, self.MIN_COVERAGE, context)
        exp_drive = self.parameterAsBool(
            parameters, self.EXPORT_DRIVE, context)
        export_metodo = self.parameterAsEnum(
            parameters, self.EXPORT_METHOD, context)
        gcs_bucket = self.parameterAsString(
            parameters, self.GCS_BUCKET, context).strip()
        local_dir = self.parameterAsString(parameters, self.LOCAL_DIR, context)
        excel_out = self.parameterAsFileOutput(parameters, self.EXCEL, context)
        grafico_out = self.parameterAsFileOutput(
            parameters, self.GRAFICO, context)

        sensor_nombre = SENSOR_LABELS[idx_sensor]
        info = SENSORES[sensor_nombre]

        if exp_drive:
            if export_metodo == 1 and not gcs_bucket:
                raise QgsProcessingException(
                    "Debe especificar un Bucket de Google Cloud Storage para exportar.")
            if export_metodo == 2 and not local_dir:
                raise QgsProcessingException(
                    "Debe especificar una Carpeta local de destino para la Descarga Directa.")

        # ── Validar campo de clase ────────────────────────────────
        fields = puntos_layer.fields()
        if fields.indexOf(campo_clase) == -1:
            raise QgsProcessingException(
                f"El campo '{campo_clase}' no existe en la capa de muestreo.\n"
                f"Campos disponibles: {[f.name() for f in fields]}"
            )

        # ── Extraer geometrias de QGIS → EPSG:4326 ────────────────
        geom_type_name = QgsWkbTypes.geometryDisplayString(
            QgsWkbTypes.geometryType(puntos_layer.wkbType())
        )
        feedback.pushInfo(
            f"📍 Extrayendo y reproyectando geometrias de muestreo ({geom_type_name})...")
        muestras = []   # lista de (geojson_dict, clase, id_obj)
        crs_capa = puntos_layer.crs()
        crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(
            crs_capa, crs_wgs84, QgsProject.instance())

        for idx_f, feature in enumerate(puntos_layer.getFeatures()):
            geom = feature.geometry()
            if geom.isNull():
                continue
            # Clonar y reproyectar la geometria completa (funciona para puntos
            # Y poligonos)
            geom_reproj = geom.__class__(geom)
            geom_reproj.transform(transform)
            # Serializar a GeoJSON dict nativo (compatible con ee.Geometry)
            geojson_dict = json.loads(geom_reproj.asJson())
            clase = str(feature.attribute(campo_clase)).strip() or 'Sin_Clase'
            muestras.append((geojson_dict, clase, idx_f))

        if not muestras:
            raise QgsProcessingException(
                "La capa de muestreo no contiene ninguna geometria valida.")

        feedback.pushInfo(
            f"  -> Se obtuvieron {len(muestras)} objeto(s) valido(s) para el analisis.")

        # ── Extent → WGS84 ────────────────────────────────────────
        ext = self.parameterAsExtent(parameters, self.EXTENT, context)
        crs_ext = self.parameterAsExtentCrs(parameters, self.EXTENT, context)

        if not crs_ext.isGeographic():
            tr = QgsCoordinateTransform(
                crs_ext, crs_wgs84, QgsProject.instance())
            pmin = tr.transform(ext.xMinimum(), ext.yMinimum())
            pmax = tr.transform(ext.xMaximum(), ext.yMaximum())
            lon_min, lat_min = pmin.x(), pmin.y()
            lon_max, lat_max = pmax.x(), pmax.y()
        else:
            lon_min, lat_min = ext.xMinimum(), ext.yMinimum()
            lon_max, lat_max = ext.xMaximum(), ext.yMaximum()

        # ── Autenticar GEE ────────────────────────────────────────
        global ee
        ee = _ensure_ee(feedback)
        _autenticar_gee(ee, feedback)

        aoi = ee.Geometry.Rectangle([lon_min, lat_min, lon_max, lat_max])

        # ── Buscar escena satelital ───────────────────────────────
        feedback.pushInfo(
            f"🔍 Buscando la escena satelital de menor nubosidad...")
        mask_fn = _mask_landsat if info['tipo'] == 'landsat' else _mask_sentinel2

        col_base = (
            ee.ImageCollection(info['collection'])
            .filterBounds(aoi)
            .filterDate(fecha_inicio, fecha_fin)
            .filterMetadata(info['cloud_prop'], 'less_than', cloud_pct)
        )

        col_filtered = _aplicar_filtros(ee, col_base, aoi, min_cov, feedback)

        # Ordenar por nubosidad (menor porcentaje primero) y seleccionar la
        # primera
        col_sorted = col_filtered.sort(info['cloud_prop'])
        n_scenes = col_sorted.size().getInfo()

        if n_scenes == 0:
            raise QgsProcessingException(
                "No se encontraron imagenes que cumplan con los criterios de fecha, nubosidad y cobertura espacial."
            )

        # Seleccionar la escena optima
        image_raw = ee.Image(col_sorted.first())
        image_masked = mask_fn(image_raw)

        # Obtener informacion de ID de escena satelital
        try:
            if info['tipo'] == 'landsat':
                id_producto = image_raw.get('LANDSAT_PRODUCT_ID').getInfo()
            else:
                id_producto = image_raw.get('PRODUCT_ID').getInfo()
        except Exception:
            id_producto = image_raw.get('system:index').getInfo()

        nube = image_raw.get(info['cloud_prop']).getInfo()
        nube_fmt = f"{float(nube):.2f}%" if nube is not None else '0.0%'
        fecha_adq = ee.Date(image_raw.get('system:time_start')
                            ).format('YYYY-MM-dd').getInfo()

        feedback.pushInfo('═' * 62)
        feedback.pushInfo(f"  ESCENA OPTIMA SELECCIONADA")
        feedback.pushInfo('═' * 62)
        feedback.pushInfo(f"  ID Escena      : {id_producto}")
        feedback.pushInfo(f"  Fecha Adquisic.: {fecha_adq}")
        feedback.pushInfo(f"  Nubosidad      : {nube_fmt}")
        feedback.pushInfo(f"  Total escenas  : {n_scenes} coincidentes")
        feedback.pushInfo('─' * 62)

        # ── Extraer firmas espectrales en GEE (Server-Side) ───────
        feedback.pushInfo("🔬 Extrayendo firmas espectrales en la nube...")
        feedback.setProgress(40)

        # Convertir geometrias QGIS (puntos o poligonos) en FeatureCollection
        # GEE
        ee_features = []
        for geojson_dict, clase, id_obj in muestras:
            # ee.Geometry acepta directamente cualquier dict GeoJSON
            ee_geom = ee.Geometry(geojson_dict)
            feat = ee.Feature(ee_geom, {'clase': clase, 'id_obj': id_obj})
            ee_features.append(feat)

        fc = ee.FeatureCollection(ee_features)

        # Extraer reflectancias — ee.Reducer.mean() funciona para puntos y poligonos:
        # · Punto   → valor del pixel individual
        # · Poligono → promedio de todos los pixeles dentro del area
        gee_bands = info['bandas_gee']
        sampled = image_masked.select(gee_bands).reduceRegions(
            collection=fc,
            reducer=ee.Reducer.mean(),
            scale=info['scale_sr']
        )

        feedback.setProgress(60)
        feedback.pushInfo(
            "📥 Descargando datos espectrales a la maquina local...")
        sampled_data = sampled.getInfo()

        # ── Procesar datos en Python ──────────────────────────────
        feedback.pushInfo("📊 Analizando firmas espectrales...")
        filas = []
        nombres_bandas = info['bandas']
        n_omitidos = 0

        for feat_gee in sampled_data['features']:
            props = feat_gee['properties']
            clase = props.get('clase', 'Sin_Clase')
            id_obj = props.get('id_obj', -1)

            fila = {'Clase': clase, 'ID_Objeto': id_obj}

            # Verificar que todos los valores de banda esten presentes (no
            # enmascarados)
            valido = True
            for band_idx, gb in enumerate(gee_bands):
                # reduceRegions con mean() devuelve el nombre de la banda como
                # clave
                raw_val = props.get(gb)
                if raw_val is None:
                    valido = False
                    break

                # Escalar los valores a reflectancia [0, 1]
                if info['tipo'] == 'landsat':
                    # Landsat C2 L2 SR: DN * 0.0000275 - 0.2
                    val = float(raw_val) * 0.0000275 - 0.2
                else:
                    # Sentinel-2 Harmonized: DN * 0.0001
                    val = float(raw_val) * 0.0001

                fila[nombres_bandas[band_idx]] = round(val, 6)

            if valido:
                filas.append(fila)
            else:
                n_omitidos += 1
                feedback.pushWarning(
                    f"  ⚠ Objeto #{id_obj} (clase: {clase}) omitido — "
                    f"pixeles enmascarados (nubes/sombras/sin datos)."
                )

        if n_omitidos:
            feedback.pushInfo(
                f"  -> {n_omitidos} objeto(s) omitido(s) por mascara de nubes o sombras.")

        if not filas:
            raise QgsProcessingException(
                "Todos los objetos de muestreo cayeron en pixeles enmascarados.\n"
                "Prueba ampliando la fecha de busqueda, aumentando el umbral de nubosidad\n"
                "o revisando la ubicacion de las geometrias de muestreo."
            )

        df = pd.DataFrame(filas)
        feedback.pushInfo(
            f"  -> Extraccion exitosa para {len(df)} objeto(s) de muestreo.")
        feedback.setProgress(75)

        # ── Estadisticas resumidas por clase ───────────────────────
        resumen_rows = []
        for clase, grupo in df.groupby('Clase'):
            fila_res = {'Clase': clase, 'N_objetos': len(grupo)}
            for nb in nombres_bandas:
                vals = grupo[nb].dropna()
                fila_res[f'{nb}_media'] = round(
                    vals.mean(), 6) if len(vals) else np.nan
                fila_res[f'{nb}_min'] = round(
                    vals.min(), 6) if len(vals) else np.nan
                fila_res[f'{nb}_max'] = round(
                    vals.max(), 6) if len(vals) else np.nan
            resumen_rows.append(fila_res)

        df_resumen = pd.DataFrame(resumen_rows)

        # ── Exportar a Excel ──────────────────────────────────────
        feedback.pushInfo(
            f"💾 Guardando reporte Excel: {
                os.path.basename(excel_out)}")
        with pd.ExcelWriter(excel_out, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Datos_por_objeto', index=False)
            df_resumen.to_excel(
                writer,
                sheet_name='Resumen_por_clase',
                index=False)

            # Hojas pivot para graficos y analisis dinamico
            pivot_media = df.groupby('Clase')[nombres_bandas].mean()
            pivot_media.to_excel(writer, sheet_name='Media_por_clase')

            pivot_min = df.groupby('Clase')[nombres_bandas].min()
            pivot_min.to_excel(writer, sheet_name='Min_por_clase')

            pivot_max = df.groupby('Clase')[nombres_bandas].max()
            pivot_max.to_excel(writer, sheet_name='Max_por_clase')

        # ── Generar Grafico ───────────────────────────────────────
        feedback.pushInfo("🖼 Generando grafico de firmas espectrales...")
        clases_ordenadas = sorted(df['Clase'].unique())
        longitudes = info['longitud']

        fig, ax = plt.subplots(figsize=(11, 7))
        ax.set_facecolor('#f8f9fa')
        fig.patch.set_facecolor('white')

        leyenda_patches = []

        for i, clase in enumerate(clases_ordenadas):
            color = COLORES[i % len(COLORES)]
            grupo = df[df['Clase'] == clase][nombres_bandas]

            media = grupo.mean().values
            vmin = grupo.min().values
            vmax = grupo.max().values

            # Banda de incertidumbre (rango minimo - maximo)
            ax.fill_between(
                longitudes, vmin, vmax,
                alpha=0.18,
                color=color,
                linewidth=0,
            )
            # Linea de promedio
            ax.plot(
                longitudes, media,
                color=color,
                marker='o',
                markersize=6,
                linewidth=2,
                linestyle='-',
                zorder=3,
            )
            # Lineas min/max punteadas
            ax.plot(
                longitudes, vmin,
                color=color,
                linestyle=':',
                linewidth=0.8,
                alpha=0.7,
                zorder=2,
            )
            ax.plot(
                longitudes, vmax,
                color=color,
                linestyle=':',
                linewidth=0.8,
                alpha=0.7,
                zorder=2,
            )

            # Agregar a la leyenda
            leyenda_patches.append(
                mpatches.Patch(color=color, label=f"{clase} (n={len(grupo)})")
            )

        # Configuracion de ejes y estetica
        ax.set_xlabel(
            'Longitud de onda (μm)',
            fontsize=11,
            fontweight='bold',
            labelpad=8)
        ax.set_ylabel(
            'Reflectancia (0.0 - 1.0)',
            fontsize=11,
            fontweight='bold',
            labelpad=8)
        ax.set_title(
            info['titulo'] +
            f"\nEscena: {id_producto} ({fecha_adq})",
            fontsize=12,
            fontweight='bold',
            pad=15)

        ax.grid(True, linestyle='--', alpha=0.5)
        ax.legend(
            handles=leyenda_patches,
            loc='upper right',
            frameon=True,
            facecolor='white',
            framealpha=0.9)

        # Ajustar los limites del eje Y para reflectancias standar
        ax.set_ylim(-0.02, 1.02)

        plt.tight_layout()
        plt.savefig(grafico_out, dpi=150)
        plt.close()
        feedback.pushInfo(
            f"  -> Grafico guardado en: {os.path.basename(grafico_out)}")

        # ── Exportar a Drive / GCS / Local (opcional) ────────────────────────
        if exp_drive:
            feedback.pushInfo(
                f"💾 Iniciando exportación de la escena multiespectral completa...")
            # Limpiar ID
            id_clean = _limpiar_nombre(
                str(id_producto).replace(
                    '-',
                    '_').replace(
                    '/',
                    '_'))
            nombre_export = _limpiar_nombre(
                f"{fecha_adq[:4]}_{info['sensor']}_MULTISPECTRAL_{id_clean}")

            if export_metodo == 2:  # Local
                total_px = _estimar_pixeles(
                    lon_min, lat_min, lon_max, lat_max, info['scale_sr'])
                if total_px > LIMITE_DIRECTO_MP:
                    feedback.pushWarning(
                        f"AVISO: El área ({
                            total_px / 1e6:.1f} MP) puede superar el límite "
                        f"de descarga directa de GEE (~48 MB). Si falla, cambia a Drive o GCS."
                    )
                out_path = os.path.join(local_dir, f"{nombre_export}.tif")
                _descarga_directa(ee, image_raw.select(gee_bands).clip(
                    aoi), info['scale_sr'], aoi, out_path, feedback)
                feedback.pushInfo(
                    f"  -> Escena exportada localmente en: {out_path}")
            else:  # Drive o GCS
                destino = info['gdrive'] if export_metodo == 0 else gcs_bucket
                task_id = _exportar_imagen(
                    ee, image_raw.select(gee_bands), nombre_export, destino,
                    info['scale_sr'], aoi, export_metodo, feedback
                )
                feedback.pushInfo(
                    f"  -> Tarea de exportacion iniciada en GEE. Task ID: {task_id}")
                dest_str = "Drive" if export_metodo == 0 else "GCS"
                feedback.pushInfo(f"  -> Destino: {dest_str} '{destino}'")

        feedback.setProgress(100)
        feedback.pushInfo('═' * 62)
        feedback.pushInfo('  FIRMA ESPECTRAL GEE COMPLETADA CON EXITO')
        feedback.pushInfo('═' * 62)
        feedback.pushInfo(f"  Excel   : {excel_out}")
        feedback.pushInfo(f"  Grafico : {grafico_out}")
        feedback.pushInfo('═' * 62)

        return {self.EXCEL: excel_out, self.GRAFICO: grafico_out}

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
