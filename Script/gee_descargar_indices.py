from ._qt_compat import qt_exec
from .geomaticape_algorithm import GeomaticapeAlgorithm
# -*- coding: utf-8 -*-
"""
gee_descargar_indices.py
Descarga indices espectrales (NDVI, SAVI, EVI, etc.) obtenidos mediante GEE
desde Google Earth Engine → Google Drive.

Logica interna:
  - Filtro por fecha, extension y nubosidad (configurable)
  - Filtro geometrico (footprint contiene el AOI)
  - Filtro de cobertura real de pixeles validos (umbral configurable)
  - Enmascaramiento de nubes y sombras antes del calculo
  - Escalado de reflectancias segun sensor a rango [0, 1]
  - Calculo del indice seleccionado en el servidor GEE
  - Exportacion individual de cada indice a Google Drive (carpeta: geomatica_indices)
  - Generacion de tabla CSV con el registro de imagenes/indices enviados

Carpeta Google Drive:
  geomatica_indices

Nombre de archivo exportado:
  {YYYY}_{SENSOR}_{INDICE}_{PRODUCT_ID_clean}_{cloud:.1f}

GeomaticaPe — Geomatica Ambiental
"""

import os
import re
import datetime

from qgis.core import (
    QgsProcessingException,
    QgsProcessingParameterExtent,
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFolderDestination,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
)

import math

# Importar dinamicamente el catalogo de indices
try:
    from .indices_espectrales import INDICES, INDEX_KEYS
except ImportError:
    import sys
    sys.path.append(os.path.dirname(__file__))
    from indices_espectrales import INDICES, INDEX_KEYS

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────
GDRIVE_FOLDER = 'geomatica_indices'
POLL_INTERVAL_S = 10
SCALE_COVERAGE = 300   # resolucion para calculo de cobertura (m)
LIMITE_DIRECTO_MP = 10_000_000  # pixeles — umbral para descarga directa

# Fórmulas GEE-compatibles correspondientes a cada índice
GEE_FORMULAS = {
    "NDVI": "(NIR - RED) / (NIR + RED)",
    "SAVI": "((NIR - RED) / (NIR + RED + 0.5)) * 1.5",
    "MSAVI": "0.5 * (2 * NIR + 1 - sqrt((2 * NIR + 1) * (2 * NIR + 1) - 8 * (NIR - RED)))",
    "EVI": "2.5 * (NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1)",
    "EVI2": "2.4 * (NIR - RED) / (NIR + RED + 1)",
    "GNDVI": "(NIR - GREEN) / (NIR + GREEN)",
    "GCI": "(NIR / GREEN) - 1",
    "NDREI": "(NIR - REDEDGE) / (NIR + REDEDGE)",
    "NDWI": "(GREEN - NIR) / (GREEN + NIR)",
    "MNDWI": "(GREEN - SWIR1) / (GREEN + SWIR1)",
    "NDMI": "(NIR - SWIR1) / (NIR + SWIR1)",
    "NBR": "(NIR - SWIR2) / (NIR + SWIR2)",
    "NBR2": "(SWIR1 - SWIR2) / (SWIR1 + SWIR2)",
    "NDSI": "(GREEN - SWIR1) / (GREEN + SWIR1)",
    "BSI": "((SWIR1 + RED) - (NIR + BLUE)) / ((SWIR1 + RED) + (NIR + BLUE))",
    "VARI": "(GREEN - RED) / (GREEN + RED - BLUE)",
    "NGRDI": "(GREEN - RED) / (GREEN + RED)",
}

# ─────────────────────────────────────────────────────────────────────────────
# Catalogo de sensores con mapeo de roles
# ─────────────────────────────────────────────────────────────────────────────
SENSORES = [
    {
        'label': 'Landsat 4 C2 L2 (SR)',
        'collection': 'LANDSAT/LT04/C02/T1_L2',
        'sensor': 'LANDSAT_4',
        'tipo': 'landsat',
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'roles': {
            'BLUE': 'SR_B1',
            'GREEN': 'SR_B2',
            'RED': 'SR_B3',
            'NIR': 'SR_B4',
            'SWIR1': 'SR_B5',
            'SWIR2': 'SR_B7',
            'REDEDGE': None,
        }
    },
    {
        'label': 'Landsat 5 C2 L2 (SR)',
        'collection': 'LANDSAT/LT05/C02/T1_L2',
        'sensor': 'LANDSAT_5',
        'tipo': 'landsat',
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'roles': {
            'BLUE': 'SR_B1',
            'GREEN': 'SR_B2',
            'RED': 'SR_B3',
            'NIR': 'SR_B4',
            'SWIR1': 'SR_B5',
            'SWIR2': 'SR_B7',
            'REDEDGE': None,
        }
    },
    {
        'label': 'Landsat 7 C2 L2 (SR)',
        'collection': 'LANDSAT/LE07/C02/T1_L2',
        'sensor': 'LANDSAT_7',
        'tipo': 'landsat',
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'roles': {
            'BLUE': 'SR_B1',
            'GREEN': 'SR_B2',
            'RED': 'SR_B3',
            'NIR': 'SR_B4',
            'SWIR1': 'SR_B5',
            'SWIR2': 'SR_B7',
            'REDEDGE': None,
        }
    },
    {
        'label': 'Landsat 8 C2 L2 (SR)',
        'collection': 'LANDSAT/LC08/C02/T1_L2',
        'sensor': 'LANDSAT_8',
        'tipo': 'landsat',
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'roles': {
            'BLUE': 'SR_B2',
            'GREEN': 'SR_B3',
            'RED': 'SR_B4',
            'NIR': 'SR_B5',
            'SWIR1': 'SR_B6',
            'SWIR2': 'SR_B7',
            'REDEDGE': None,
        }
    },
    {
        'label': 'Landsat 9 C2 L2 (SR)',
        'collection': 'LANDSAT/LC09/C02/T1_L2',
        'sensor': 'LANDSAT_9',
        'tipo': 'landsat',
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'roles': {
            'BLUE': 'SR_B2',
            'GREEN': 'SR_B3',
            'RED': 'SR_B4',
            'NIR': 'SR_B5',
            'SWIR1': 'SR_B6',
            'SWIR2': 'SR_B7',
            'REDEDGE': None,
        }
    },
    {
        'label': 'Sentinel-2 C2L2 (SR Harmonized)',
        'collection': 'COPERNICUS/S2_SR_HARMONIZED',
        'sensor': 'SENTINEL2_L2A',
        'tipo': 'sentinel2',
        'cloud_prop': 'CLOUDY_PIXEL_PERCENTAGE',
        'scale_sr': 10,
        'roles': {
            'BLUE': 'B2',
            'GREEN': 'B3',
            'RED': 'B4',
            'REDEDGE': 'B5',
            'NIR': 'B8',
            'SWIR1': 'B11',
            'SWIR2': 'B12',
        }
    },
    {
        'label': 'Sentinel-2 C2L1 (TOA Harmonized)',
        'collection': 'COPERNICUS/S2_HARMONIZED',
        'sensor': 'SENTINEL2_L1C',
        'tipo': 'sentinel2',
        'cloud_prop': 'CLOUDY_PIXEL_PERCENTAGE',
        'scale_sr': 10,
        'roles': {
            'BLUE': 'B2',
            'GREEN': 'B3',
            'RED': 'B4',
            'REDEDGE': 'B5',
            'NIR': 'B8',
            'SWIR1': 'B11',
            'SWIR2': 'B12',
        }
    },
]

SENSOR_LABELS = [s['label'] for s in SENSORES]

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


def get_mask_landsat(do_clouds, do_cirrus, do_shadows, do_snow, do_water):
    def _mask(image):
        import ee
        qa = image.select('QA_PIXEL')
        mask = ee.Image.constant(1)
        if do_clouds:
            mask = mask.And(qa.bitwiseAnd(1 << 3).eq(0))
        if do_shadows:
            mask = mask.And(qa.bitwiseAnd(1 << 4).eq(0))
        if do_snow:
            mask = mask.And(qa.bitwiseAnd(1 << 5).eq(0))
        if do_water:
            mask = mask.And(qa.bitwiseAnd(1 << 7).eq(0))
        if do_cirrus:
            mask = mask.And(qa.bitwiseAnd(1 << 2).eq(0))
        return image.updateMask(mask)
    return _mask


def get_mask_sentinel2_l2a(
        do_clouds, do_cirrus, do_shadows, do_snow, do_water):
    def _mask(image):
        import ee
        scl = image.select('SCL')
        mask = ee.Image.constant(1)
        if do_clouds:
            mask = mask.And(scl.neq(8)).And(scl.neq(9))
        if do_shadows:
            mask = mask.And(scl.neq(3))
        if do_snow:
            mask = mask.And(scl.neq(11))
        if do_water:
            mask = mask.And(scl.neq(6))
        if do_cirrus:
            mask = mask.And(scl.neq(10))
        return image.updateMask(mask)
    return _mask


def get_mask_sentinel2_l1c(do_clouds, do_cirrus):
    def _mask(image):
        import ee
        qa = image.select('QA60')
        mask = ee.Image.constant(1)
        if do_clouds:
            mask = mask.And(qa.bitwiseAnd(1 << 10).eq(0))
        if do_cirrus:
            mask = mask.And(qa.bitwiseAnd(1 << 11).eq(0))
        return image.updateMask(mask)
    return _mask


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
    Reporta estadisticas de filtrado.
    """
    n_orig = collection.size().getInfo()
    feedback.pushInfo(f"  Imagenes antes del filtro         : {n_orig}")

    col_l1 = _filtro_geometrico(collection, aoi)
    n_l1 = col_l1.size().getInfo()
    feedback.pushInfo(
        f"  Tras filtro geometrico (L1)       : {n_l1}  (−{
            n_orig - n_l1})")

    col_l2 = col_l1.map(lambda img: _calcular_cobertura(ee, img, aoi))
    col_l2 = col_l2.filter(ee.Filter.gte('aoi_coverage', min_coverage))
    n_l2 = col_l2.size().getInfo()
    feedback.pushInfo(
        f"  Tras filtro cobertura (≥{min_coverage:.0%}, L2): {n_l2}  "
        f"(−{n_l1 - n_l2})"
    )
    feedback.pushInfo(f"  Imagenes VALIDAS para exportar    : {n_l2}")

    return col_l2.sort('system:time_start')

    return col_l2.sort('system:time_start')


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
    """
    Inicia una tarea Export.image.toDrive o toCloudStorage para una imagen.
    Devuelve el task ID.
    """
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

class GEEDescargarIndices(GeomaticapeAlgorithm):
    _algorithm_name = "gee_descargar_indices"
    _icon_name = "default.png"

    EXTENT = 'EXTENT'
    SENSOR = 'SENSOR'
    INDEX_TYPE = 'INDEX_TYPE'
    DATE_START = 'DATE_START'
    DATE_END = 'DATE_END'
    CLOUD_PCT = 'CLOUD_PCT'
    MIN_COVERAGE = 'MIN_COVERAGE'
    EXPORT_METHOD = 'EXPORT_METHOD'
    GCS_BUCKET = 'GCS_BUCKET'
    LOCAL_DIR = 'LOCAL_DIR'
    MASK_CLOUDS = 'MASK_CLOUDS'
    MASK_CIRRUS = 'MASK_CIRRUS'
    MASK_SHADOWS = 'MASK_SHADOWS'
    MASK_SNOW = 'MASK_SNOW'
    MASK_WATER = 'MASK_WATER'
    OUTPUT_TABLE = 'OUTPUT_TABLE'

    def displayName(self):
        return self.tr('Descargar indices espectrales (GEE)')

    def group(self):
        return self.tr('Descarga GEE')

    def groupId(self):
        return 'descarga_gee'

    def tags(self):
        return [
            'landsat', 'sentinel', 'sentinel-2', 'gee', 'google earth engine',
            'google drive', 'descarga', 'indices', 'espectrales', 'ndvi', 'evi',
        ]

    def shortHelpString(self):
        catalogo = ''.join(
            f'<li><b>{k}</b> — <small>{
                INDICES[k]["name"]} | Formula: <code>{
                INDICES[k]["formula"]}</code></small></li>'
            for k in INDEX_KEYS
        )
        return f"""
<b>Descargar indices espectrales desde Google Earth Engine</b><br><br>

Calcula el indice espectral seleccionado directamente en los servidores de <b>Google Earth Engine</b> y exporta el resultado a <b>Google Drive</b> (carpeta: <code>geomatica_indices</code>) como un raster monocanal Float32.<br><br>

<b>Evita la descarga de pesadas bandas multiespectrales complejas.</b><br><br>

<b>Nombre de archivo exportado:</b><br>
<code>YYYY_SENSOR_INDICE_ID_PRODUCTO_nube</code><br><br>

<b>Filtros aplicados:</b><br>
<ol>
  <li>Fecha y extension del AOI</li>
  <li>Nubosidad maxima (configurable)</li>
  <li>Filtro geometrico: footprint contiene el AOI completo</li>
  <li>Filtro de cobertura real de pixeles validos (umbral configurable)</li>
  <li>Enmascaramiento de nubes y sombras antes del calculo</li>
</ol>

<b>Indices disponibles ({len(INDEX_KEYS)}):</b><br><ul>{catalogo}</ul>
"""

    def initAlgorithm(self, config=None):

        # ── Extension ────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT,
            self.tr('Extension del area de interes (AOI)')
        ))

        # ── Sensor ───────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterEnum(
            self.SENSOR,
            self.tr('Sensor / Coleccion'),
            options=SENSOR_LABELS,
            defaultValue=3   # Landsat 8 por defecto
        ))

        # ── Indice Espectral ─────────────────────────────────────
        labels_indices = [f"{k}  -  {INDICES[k]['name']}" for k in INDEX_KEYS]
        self.addParameter(QgsProcessingParameterEnum(
            self.INDEX_TYPE,
            self.tr('Indice espectral a descargar'),
            options=labels_indices,
            defaultValue=0   # NDVI por defecto
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

        # ── Enmascaramiento ──────────────────────────────────────
        self.addParameter(QgsProcessingParameterBoolean(
            self.MASK_CLOUDS, self.tr('Enmascarar Nubes'), defaultValue=False
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MASK_CIRRUS, self.tr('Enmascarar Cirros'), defaultValue=False
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MASK_SHADOWS, self.tr('Enmascarar Sombras de nubes'), defaultValue=False
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MASK_SNOW, self.tr('Enmascarar Nieve/Hielo'), defaultValue=False
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MASK_WATER, self.tr('Enmascarar Agua'), defaultValue=False
        ))

        # ── Método de Exportación ────────────────────────────────
        self.addParameter(QgsProcessingParameterEnum(
            self.EXPORT_METHOD,
            self.tr('Método de exportación'),
            options=[
                'Google Drive',
                'Google Cloud Storage (GCS)',
                'Descarga Directa (imágenes pequeñas)'],
            defaultValue=2
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

        # ── Tabla de registro ────────────────────────────────────
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_TABLE,
            self.tr('Tabla de registro (CSV)'),
            fileFilter='CSV (*.csv)',
            optional=True
        ))

    # ─────────────────────────────────────────────────────────────
    def processAlgorithm(self, parameters, context, feedback):
        import csv

        # ── Leer parametros ───────────────────────────────────────
        idx_sensor = self.parameterAsEnum(parameters, self.SENSOR, context)
        idx_index = self.parameterAsEnum(parameters, self.INDEX_TYPE, context)
        fecha_inicio = self.parameterAsString(
            parameters, self.DATE_START, context).strip()
        fecha_fin = self.parameterAsString(
            parameters, self.DATE_END, context).strip()
        cloud_pct = self.parameterAsDouble(parameters, self.CLOUD_PCT, context)
        min_cov = self.parameterAsDouble(
            parameters, self.MIN_COVERAGE, context)
        export_metodo = self.parameterAsEnum(
            parameters, self.EXPORT_METHOD, context)
        gcs_bucket = self.parameterAsString(
            parameters, self.GCS_BUCKET, context).strip()
        local_dir = self.parameterAsString(parameters, self.LOCAL_DIR, context)
        do_clouds = self.parameterAsBool(parameters, self.MASK_CLOUDS, context)
        do_cirrus = self.parameterAsBool(parameters, self.MASK_CIRRUS, context)
        do_shadows = self.parameterAsBool(
            parameters, self.MASK_SHADOWS, context)
        do_snow = self.parameterAsBool(parameters, self.MASK_SNOW, context)
        do_water = self.parameterAsBool(parameters, self.MASK_WATER, context)
        csv_path = self.parameterAsFileOutput(
            parameters, self.OUTPUT_TABLE, context)

        info = SENSORES[idx_sensor]
        idx_code = INDEX_KEYS[idx_index]
        index_def = INDICES[idx_code]
        gee_formula = GEE_FORMULAS[idx_code]

        if export_metodo == 1 and not gcs_bucket:
            raise QgsProcessingException(
                "Debe especificar un Bucket de Google Cloud Storage.")
        if export_metodo == 2 and not local_dir:
            feedback.pushWarning(
                "No se especificó una Carpeta local para la Descarga Directa. Cambiando a Google Drive por defecto.")
            export_metodo = 0

        # ── Validaciones basicas ──────────────────────────────────
        bands_needed = index_def['bands']
        missing_roles = [
            r for r in bands_needed if info['roles'].get(r) is None]
        if missing_roles:
            raise QgsProcessingException(
                f"El sensor seleccionado ({
                    info['label']}) no posee las bandas necesarias "
                f"para calcular el indice {idx_code} (falta: {
                    ', '.join(missing_roles)})."
            )

        try:
            datetime.datetime.strptime(fecha_inicio, '%Y-%m-%d')
            datetime.datetime.strptime(fecha_fin, '%Y-%m-%d')
        except ValueError:
            raise QgsProcessingException(
                "Formato de fecha incorrecto. Use YYYY-MM-DD (ej: 2020-06-15)."
            )

        if fecha_inicio >= fecha_fin:
            raise QgsProcessingException(
                "La fecha de inicio debe ser anterior a la fecha fin."
            )

        # ── Extent → WGS84 ────────────────────────────────────────
        ext = self.parameterAsExtent(parameters, self.EXTENT, context)
        crs_ext = self.parameterAsExtentCrs(parameters, self.EXTENT, context)
        crs_geo = QgsCoordinateReferenceSystem('EPSG:4326')

        if not crs_ext.isGeographic():
            tr = QgsCoordinateTransform(
                crs_ext, crs_geo, QgsProject.instance())
            pmin = tr.transform(ext.xMinimum(), ext.yMinimum())
            pmax = tr.transform(ext.xMaximum(), ext.yMaximum())
            lon_min, lat_min = pmin.x(), pmin.y()
            lon_max, lat_max = pmax.x(), pmax.y()
        else:
            lon_min, lat_min = ext.xMinimum(), ext.yMinimum()
            lon_max, lat_max = ext.xMaximum(), ext.yMaximum()

        # ── Resumen inicial ───────────────────────────────────────
        feedback.pushInfo('═' * 62)
        feedback.pushInfo('  DESCARGA DE INDICE ESPECTRAL GEE')
        feedback.pushInfo('═' * 62)
        feedback.pushInfo(f"  Sensor         : {info['label']}")
        feedback.pushInfo(
            f"  Indice         : {idx_code} - {index_def['name']}")
        feedback.pushInfo(f"  Formula        : {gee_formula}")
        feedback.pushInfo(f"  Coleccion GEE  : {info['collection']}")
        feedback.pushInfo(f"  Periodo        : {fecha_inicio}  a  {fecha_fin}")
        feedback.pushInfo(f"  Nubosidad max  : {cloud_pct:.0f}%")
        feedback.pushInfo(f"  Cobertura min  : {min_cov:.0%}")
        feedback.pushInfo(f"  Bbox WGS84     : [{lon_min:.5f}, {lat_min:.5f}, "
                          f"{lon_max:.5f}, {lat_max:.5f}]")
        if export_metodo == 0:
            feedback.pushInfo(
                f"  Destino        : Google Drive ({GDRIVE_FOLDER})")
        elif export_metodo == 1:
            feedback.pushInfo(
                f"  Destino        : Google Cloud Storage ({gcs_bucket})")
        else:
            feedback.pushInfo(f"  Destino        : Local ({local_dir})")
        feedback.pushInfo('─' * 62)

        # Verificar limite de pixeles para Descarga Directa
        if export_metodo == 2:
            total_px = _estimar_pixeles(
                lon_min, lat_min, lon_max, lat_max, info['scale_sr'])
            if total_px > LIMITE_DIRECTO_MP:
                feedback.pushWarning(
                    f"AVISO: El área ({
                        total_px / 1e6:.1f} MP) puede superar el límite "
                    f"de descarga directa de GEE (~48 MB). Si falla, cambia a Drive o GCS."
                )

        # ── Autenticar GEE ────────────────────────────────────────
        global ee
        ee = _ensure_ee(feedback)
        _autenticar_gee(ee, feedback)

        # ── AOI y coleccion base ───────────────────────────────────
        aoi = ee.Geometry.Rectangle([lon_min, lat_min, lon_max, lat_max])

        if info['tipo'] == 'landsat':
            mask_fn = get_mask_landsat(
                do_clouds, do_cirrus, do_shadows, do_snow, do_water)
        elif info['sensor'] == 'SENTINEL2_L2A':
            mask_fn = get_mask_sentinel2_l2a(
                do_clouds, do_cirrus, do_shadows, do_snow, do_water)
        else:
            mask_fn = get_mask_sentinel2_l1c(do_clouds, do_cirrus)

        col_base = (
            ee.ImageCollection(info['collection'])
            .filterBounds(aoi)
            .filterDate(fecha_inicio, fecha_fin)
            .filterMetadata(info['cloud_prop'], 'less_than', cloud_pct)
            .map(mask_fn)
        )

        feedback.pushInfo("Aplicando filtros de cobertura...")
        col = _aplicar_filtros(ee, col_base, aoi, min_cov, feedback)

        n_imagenes = col.size().getInfo()

        if n_imagenes == 0:
            raise QgsProcessingException(
                "No se encontraron imagenes que cumplan los criterios.\n"
                "Prueba ampliar el rango de fechas, aumentar la nubosidad maxima\n"
                "o reducir el umbral de cobertura minima."
            )

        feedback.pushInfo(f"\nTotal de imagenes a exportar: {n_imagenes}")
        feedback.pushInfo('─' * 62)

        # ── Extraer metadatos en 1 sola llamada (Optimizacion) ───
        def extract_meta(img):
            fecha = ee.Date(img.get('system:time_start')).format('YYYY-MM-dd')
            id_prod = img.get(
                'LANDSAT_PRODUCT_ID') if info['tipo'] == 'landsat' else img.get('PRODUCT_ID')
            nube = img.get(info['cloud_prop'])
            return ee.Feature(None, {
                'fecha': fecha,
                'id_producto': id_prod,
                'nube': nube
            })

        feedback.pushInfo("Extrayendo metadatos del servidor...")
        meta_fc = col.map(extract_meta)
        meta_features = meta_fc.getInfo().get('features', [])

        # ── Iterar y exportar ─────────────────────────────────────
        lista = col.toList(n_imagenes)
        registros = []

        for i, feat in enumerate(meta_features):

            if feedback.isCanceled():
                raise QgsProcessingException(
                    "Proceso cancelado por el usuario.")

            props = feat.get('properties', {})
            fecha = props.get('fecha', '')
            anio = fecha[:4] if fecha else ''
            id_producto = props.get('id_producto', '')
            nube = props.get('nube')

            img = ee.Image(lista.get(i))
            nube_fmt = f"{float(nube):.1f}" if nube is not None else '0.0'

            # ── Limpiar ID para nombre de archivo ─────────────────
            id_clean = _limpiar_nombre(
                str(id_producto).replace('-', '_').replace('/', '_')
            )

            # ── Escalar bandas para calculo ───────────────────────
            scaled_bands = {}
            if info['tipo'] == 'landsat':
                # Landsat C2 L2 SR scaling
                for role in bands_needed:
                    gee_band = info['roles'][role]
                    scaled_bands[role] = img.select(
                        gee_band).multiply(0.0000275).add(-0.2)
            else:
                # Sentinel-2 Harmonized (L1C / L2A) scaling
                for role in bands_needed:
                    gee_band = info['roles'][role]
                    scaled_bands[role] = img.select(gee_band).multiply(0.0001)

            # ── Calcular indice en GEE usando expression ─────────
            img_index = img.expression(
                gee_formula, scaled_bands).rename(
                [idx_code]).clip(aoi)

            # Nombre de archivo
            nombre_index = _limpiar_nombre(
                f"{anio}_{info['sensor']}_{idx_code}_{id_clean}_{nube_fmt}"
            )

            if export_metodo == 2:  # Local
                out_path = os.path.join(local_dir, f"{nombre_index}.tif")
                try:
                    _descarga_directa(
                        ee, img_index, info['scale_sr'], aoi, out_path, feedback)
                    task_id = 'LOCAL'
                    feedback.pushInfo(
                        f"  [{i + 1:03d}/{n_imagenes}] {idx_code:<5} → {nombre_index} (Descargado)")
                except Exception as e:
                    feedback.pushWarning(
                        f"Descarga directa falló: {e}. Reintentando con Google Drive...")
                    destino = GDRIVE_FOLDER
                    task_id = _exportar_imagen(
                        ee, img_index, nombre_index, destino,
                        info['scale_sr'], aoi, 0, feedback
                    )
                    feedback.pushInfo(
                        f"  [{i + 1:03d}/{n_imagenes}] {idx_code:<5} → {nombre_index}  "
                        f"(task: {task_id})"
                    )
            else:
                destino = GDRIVE_FOLDER if export_metodo == 0 else gcs_bucket
                task_id = _exportar_imagen(
                    ee, img_index, nombre_index, destino,
                    info['scale_sr'], aoi, export_metodo, feedback
                )
                feedback.pushInfo(
                    f"  [{i + 1:03d}/{n_imagenes}] {idx_code:<5} → {nombre_index}  "
                    f"(task: {task_id})"
                )

            # ── Registro ──────────────────────────────────────────
            dest_str = "Local" if export_metodo == 2 else (
                "GCS" if export_metodo == 1 else GDRIVE_FOLDER)
            registros.append({
                'N': i + 1,
                'ID_PRODUCTO': id_producto,
                'SENSOR': info['sensor'],
                'FECHA': fecha,
                'ANIO': anio,
                'NUBOSIDAD': nube_fmt,
                'INDICE': idx_code,
                'NOMBRE_INDICE': nombre_index,
                'TASK_ID': task_id or '',
                'DESTINO': dest_str,
            })

            feedback.setProgress(int((i + 1) * 100 / n_imagenes))

        # ── Guardar tabla CSV ─────────────────────────────────────
        if csv_path and csv_path != 'TEMPORARY_OUTPUT':
            try:
                campos = [
                    'N', 'ID_PRODUCTO', 'SENSOR', 'FECHA', 'ANIO',
                    'NUBOSIDAD', 'INDICE', 'NOMBRE_INDICE',
                    'TASK_ID', 'DESTINO'
                ]
                with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=campos)
                    writer.writeheader()
                    writer.writerows(registros)
                feedback.pushInfo(f"\nTabla CSV guardada en: {csv_path}")
            except Exception as ex:
                feedback.pushWarning(f"No se pudo guardar la tabla CSV: {ex}")

        # ── Resumen final ─────────────────────────────────────────
        feedback.pushInfo('')
        feedback.pushInfo('═' * 62)
        feedback.pushInfo('  EXPORTACION DE INDICES INICIADA EXITOSAMENTE')
        feedback.pushInfo('═' * 62)
        feedback.pushInfo(f"  Sensor         : {info['label']}")
        feedback.pushInfo(
            f"  Indice         : {idx_code} - {index_def['name']}")
        feedback.pushInfo(f"  Periodo        : {fecha_inicio} → {fecha_fin}")
        feedback.pushInfo(f"  Imagenes export: {n_imagenes}")
        if export_metodo == 0:
            feedback.pushInfo(
                f"  Destino        : Google Drive ({GDRIVE_FOLDER})")
        elif export_metodo == 1:
            feedback.pushInfo(
                f"  Destino        : Google Cloud Storage ({gcs_bucket})")
        else:
            feedback.pushInfo(f"  Destino        : Local ({local_dir})")
        feedback.pushInfo('')
        if export_metodo in [0, 1]:
            feedback.pushInfo('  Monitorea el progreso en:')
            feedback.pushInfo('    https://code.earthengine.google.com/tasks')
        feedback.pushInfo('')
        feedback.pushInfo('═' * 62)

        return {self.OUTPUT_TABLE: csv_path}

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
