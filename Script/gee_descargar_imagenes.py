from ._qt_compat import qt_exec
from .geomaticape_algorithm import GeomaticapeAlgorithm
# -*- coding: utf-8 -*-
"""
gee_descargar_imagenes.py
Descarga imagenes Landsat (4, 5, 7, 8, 9) o Sentinel-2 (C2L2 / C2L1)
desde Google Earth Engine → Google Drive.

Logica interna:
  - Filtro por fecha, extension y nubosidad (configurable)
  - Filtro geometrico (footprint contiene el AOI)
  - Filtro de cobertura real de pixeles validos (umbral configurable)
  - Enmascaramiento de nubes y sombras (hardcoded, bit QA_PIXEL / QA60)
  - Exportacion individual de cada imagen a Google Drive
  - Generacion de tabla CSV con el registro de imagenes enviadas

Carpeta Google Drive:
  Landsat   → Pedido_Geomatica_Landsat
  Sentinel2 → Pedido_Geomatica_Sentinel2

Nombre de archivo exportado:
  Landsat SR  : {YYYY}_{SENSOR}_RS_{LANDSAT_PRODUCT_ID_clean}_{cloud:.1f}
  Landsat TIRS: {YYYY}_{SENSOR}_TIRS_{LANDSAT_PRODUCT_ID_clean}_{cloud:.1f}
  Sentinel    : {YYYY}_{SENSOR}_RS_{PRODUCT_ID_clean}_{cloud:.1f}

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
    QgsProcessingParameterBoolean,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterFolderDestination,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProcessingParameterFeatureSource,
    QgsProcessing,
)

import math

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────
GDRIVE_LANDSAT = 'Pedido_Geomatica_Landsat'
GDRIVE_SENTINEL = 'Pedido_Geomatica_Sentinel2'
POLL_INTERVAL_S = 10
SCALE_COVERAGE = 300   # resolucion para calculo de cobertura (m)
LIMITE_DIRECTO_MP = 10_000_000  # pixeles — umbral para descarga directa

# ─────────────────────────────────────────────────────────────────────────────
# Catalogo de sensores
# ─────────────────────────────────────────────────────────────────────────────
SENSORES = [
    {
        'label': 'Landsat 4 C2 L2 (SR)',
        'collection': 'LANDSAT/LT04/C02/T1_L2',
        'sensor': 'LANDSAT_4',
        'tipo': 'landsat',
        'bandas_sr': ['SR_B1', 'SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B7'],
        'bandas_tirs': ['ST_B6'],
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'scale_tirs': 30,
        'gdrive': GDRIVE_LANDSAT,
    },
    {
        'label': 'Landsat 5 C2 L2 (SR)',
        'collection': 'LANDSAT/LT05/C02/T1_L2',
        'sensor': 'LANDSAT_5',
        'tipo': 'landsat',
        'bandas_sr': ['SR_B1', 'SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B7'],
        'bandas_tirs': ['ST_B6'],
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'scale_tirs': 30,
        'gdrive': GDRIVE_LANDSAT,
    },
    {
        'label': 'Landsat 7 C2 L2 (SR)',
        'collection': 'LANDSAT/LE07/C02/T1_L2',
        'sensor': 'LANDSAT_7',
        'tipo': 'landsat',
        'bandas_sr': ['SR_B1', 'SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B7'],
        'bandas_tirs': ['ST_B6'],
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'scale_tirs': 30,
        'gdrive': GDRIVE_LANDSAT,
    },
    {
        'label': 'Landsat 8 C2 L2 (SR)',
        'collection': 'LANDSAT/LC08/C02/T1_L2',
        'sensor': 'LANDSAT_8',
        'tipo': 'landsat',
        'bandas_sr': ['SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B6', 'SR_B7'],
        'bandas_tirs': ['ST_B10'],
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'scale_tirs': 30,
        'gdrive': GDRIVE_LANDSAT,
    },
    {
        'label': 'Landsat 9 C2 L2 (SR)',
        'collection': 'LANDSAT/LC09/C02/T1_L2',
        'sensor': 'LANDSAT_9',
        'tipo': 'landsat',
        'bandas_sr': ['SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B6', 'SR_B7'],
        'bandas_tirs': ['ST_B10'],
        'cloud_prop': 'CLOUD_COVER',
        'scale_sr': 30,
        'scale_tirs': 30,
        'gdrive': GDRIVE_LANDSAT,
    },
    {
        'label': 'Sentinel-2 C2L2 (SR Harmonized)',
        'collection': 'COPERNICUS/S2_SR_HARMONIZED',
        'sensor': 'SENTINEL2_L2A',
        'tipo': 'sentinel2',
        'bandas_sr': ['B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B11', 'B12'],
        'bandas_tirs': [],
        'cloud_prop': 'CLOUDY_PIXEL_PERCENTAGE',
        'scale_sr': 10,
        'scale_tirs': None,
        'gdrive': GDRIVE_SENTINEL,
    },
    {
        'label': 'Sentinel-2 C2L1 (TOA Harmonized)',
        'collection': 'COPERNICUS/S2_HARMONIZED',
        'sensor': 'SENTINEL2_L1C',
        'tipo': 'sentinel2',
        'bandas_sr': ['B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B11', 'B12'],
        'bandas_tirs': [],
        'cloud_prop': 'CLOUDY_PIXEL_PERCENTAGE',
        'scale_sr': 10,
        'scale_tirs': None,
        'gdrive': GDRIVE_SENTINEL,
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


def _make_mask_landsat(mask_opts):
    """
    Fabrica de funciones de enmascaramiento para Landsat C2 L2.
    Utilizamos los bits de Confianza (Medium / High) para mayor rigurosidad:
      - Nubes: Bit 9 (Medium/High Cloud Confidence) + Bit 1 (Dilated Cloud)
      - Cirros: Bit 15 (Medium/High Cirrus Confidence)
      - Sombras: Bit 11 (Medium/High Shadow Confidence)
      - Nieve/Hielo: Bit 13 (Medium/High Snow/Ice Confidence)
      - Agua: Bit 7 (Water)
    """
    bits_to_check = []
    if mask_opts.get('clouds'):
        bits_to_check.extend([1 << 9, 1 << 1])
    if mask_opts.get('cirrus'):
        bits_to_check.append(1 << 15)
    if mask_opts.get('shadows'):
        bits_to_check.append(1 << 11)
    if mask_opts.get('snow_ice'):
        bits_to_check.append(1 << 13)
    if mask_opts.get('water'):
        bits_to_check.append(1 << 7)

    def _mask(image):
        if not bits_to_check:
            return image
        qa = image.select('QA_PIXEL')
        mask = qa.bitwiseAnd(bits_to_check[0]).eq(0)
        for bit in bits_to_check[1:]:
            mask = mask.And(qa.bitwiseAnd(bit).eq(0))

        # Opcional: asegurarnos de que la imagen quede explícitamente sin datos (transparente)
        # actualizando su máscara a nivel global.
        return image.updateMask(mask)

    return _mask


def _make_mask_sentinel2(mask_opts, is_l2a):
    """
    Fabrica de funciones de enmascaramiento para Sentinel-2.
    Usa SCL si es L2A (mucha mayor precision) y QA60 como alternativa para L1C.
    """
    use_clouds = mask_opts.get('clouds', False)
    use_cirrus = mask_opts.get('cirrus', False)
    use_shadows = mask_opts.get('shadows', False)
    use_snow_ice = mask_opts.get('snow_ice', False)
    use_water = mask_opts.get('water', False)

    def _mask(image):
        conditions = []
        if is_l2a:
            scl = image.select('SCL')
            if use_clouds:
                conditions.append(
                    scl.neq(8).And(
                        scl.neq(9)))  # Cloud Medium / High
            if use_cirrus:
                conditions.append(scl.neq(10))  # Thin Cirrus
            if use_shadows:
                conditions.append(scl.neq(3))  # Cloud Shadows
            if use_snow_ice:
                conditions.append(scl.neq(11))  # Snow / Ice
            if use_water:
                conditions.append(scl.neq(6))  # Water
        else:
            if use_clouds or use_cirrus:
                qa = image.select('QA60')
                if use_clouds:
                    conditions.append(qa.bitwiseAnd(1 << 10).eq(0))
                if use_cirrus:
                    conditions.append(qa.bitwiseAnd(1 << 11).eq(0))

        if not conditions:
            return image

        mask = conditions[0]
        for c in conditions[1:]:
            mask = mask.And(c)
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

# ─────────────────────────────────────────────────────────────────────────────
# Exportacion
# ─────────────────────────────────────────────────────────────────────────────


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

class GEEDescargarImagenes(GeomaticapeAlgorithm):
    _algorithm_name = "gee_descargar_imagenes"
    _icon_name = "default.png"

    EXTENT = 'EXTENT'
    VECTOR_AOI = 'VECTOR_AOI'
    SENSOR = 'SENSOR'
    DATE_START = 'DATE_START'
    DATE_END = 'DATE_END'
    CLOUD_PCT = 'CLOUD_PCT'
    THERMAL = 'THERMAL'
    MIN_COVERAGE = 'MIN_COVERAGE'
    EXPORT_METHOD = 'EXPORT_METHOD'
    GCS_BUCKET = 'GCS_BUCKET'
    LOCAL_DIR = 'LOCAL_DIR'
    OUTPUT_TABLE = 'OUTPUT_TABLE'
    MASK_CLOUDS = 'MASK_CLOUDS'
    MASK_CIRRUS = 'MASK_CIRRUS'
    MASK_SHADOWS = 'MASK_SHADOWS'
    MASK_SNOW_ICE = 'MASK_SNOW_ICE'
    MASK_WATER = 'MASK_WATER'

    def displayName(self):
        return self.tr('Descargar imagenes Landsat / Sentinel-2')

    def group(self):
        return self.tr('Descarga GEE')

    def groupId(self):
        return 'descarga_gee'

    def tags(self):
        return [
            'landsat', 'sentinel', 'sentinel-2', 'gee', 'google earth engine',
            'google drive', 'descarga', 'imagenes', 'satelite', 'reflectancia',
            'superficie', 'toa', 'sr', 'termica', 'tirs', 'nubosidad',
        ]

    def shortHelpString(self):
        catalogo = ''.join(
            f'<li><b>{s["label"]}</b> — '
            f'<small>Bandas SR: {", ".join(s["bandas_sr"])}'
            + (f' | TIRS: {", ".join(s["bandas_tirs"])}' if s['bandas_tirs'] else '')
            + f' | Escala: {s["scale_sr"]} m</small></li>'
            for s in SENSORES
        )
        return f"""
<b>Descargar imagenes Landsat o Sentinel-2 desde Google Earth Engine</b><br><br>

Exporta cada imagen individualmente a <b>Google Drive</b>. Ofrece enmascaramiento
configurable de nubes, cirros, sombras (Landsat), nieve/hielo y agua (desactivado
por defecto; activa las capas que necesites).<br><br>

<b>Carpetas de salida en Google Drive:</b><br>
<ul>
  <li>Landsat   → <code>Pedido_Geomatica_Landsat</code></li>
  <li>Sentinel2 → <code>Pedido_Geomatica_Sentinel2</code></li>
</ul>

<b>Nombre de cada imagen exportada:</b><br>
<ul>
  <li>Landsat SR  : <code>YYYY_SENSOR_RS_LANDSAT_PRODUCT_ID_nube</code></li>
  <li>Landsat TIRS: <code>YYYY_SENSOR_TIRS_LANDSAT_PRODUCT_ID_nube</code></li>
  <li>Sentinel    : <code>YYYY_SENSOR_RS_PRODUCT_ID_nube</code></li>
</ul>

<b>Filtros aplicados:</b><br>
<ol>
  <li>Fecha y extension del AOI</li>
  <li>Nubosidad maxima (configurable)</li>
  <li>Filtro geometrico: footprint contiene el AOI completo</li>
  <li>Filtro de cobertura real de pixeles validos (umbral configurable)</li>
  <li>Enmascaramiento configurable: nubes, cirros, sombras (Landsat), nieve/hielo, agua</li>
</ol>

<b>Sensores disponibles:</b><br><ul>{catalogo}</ul>

<b>Configuracion inicial (una sola vez):</b><br>
<ol>
  <li>Cuenta aprobada en
      <a href="https://earthengine.google.com">earthengine.google.com</a></li>
  <li><code>python -m pip install earthengine-api</code></li>
  <li>En la Consola Python de QGIS:<br>
      <code>import ee</code><br>
      <code>ee.Authenticate()</code><br>
      <code>ee.Initialize(project='mi-proyecto-gee')</code>
  </li>
</ol>
"""

    def initAlgorithm(self, config=None):

        # ── AOI Vectorial ─────────────────────────────────────────
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.VECTOR_AOI,
            self.tr('AOI Vectorial (Opcional - Reemplaza la extension)'),
            types=[QgsProcessing.TypeVectorPolygon],
            optional=True
        ))

        # ── Extension ────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT,
            self.tr('Extension del area de interes (Se ignora si hay vector)'),
            optional=True
        ))

        # ── Sensor ───────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterEnum(
            self.SENSOR,
            self.tr('Sensor / Coleccion'),
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

        # ── Banda termica ────────────────────────────────────────
        self.addParameter(QgsProcessingParameterBoolean(
            self.THERMAL,
            self.tr('Incluir banda termica (TIRS) — solo Landsat'),
            defaultValue=False
        ))

        # ── Enmascaramiento de pixeles (todos desactivados por defecto) ─────
        self.addParameter(QgsProcessingParameterBoolean(
            self.MASK_CLOUDS,
            self.tr('Enmascarar nubes (QA_PIXEL bit 3 / QA60 bit 10)'),
            defaultValue=False,
            optional=True
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MASK_CIRRUS,
            self.tr('Enmascarar cirros (QA_PIXEL bit 2 / QA60 bit 11)'),
            defaultValue=False,
            optional=True
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MASK_SHADOWS,
            self.tr('Enmascarar sombras de nubes (QA_PIXEL bit 4 — solo Landsat)'),
            defaultValue=False,
            optional=True
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MASK_SNOW_ICE,
            self.tr(
                'Enmascarar nieve/hielo (QA_PIXEL bit 5 / SCL=11 en Sentinel-2 L2A)'),
            defaultValue=False,
            optional=True
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MASK_WATER,
            self.tr('Enmascarar agua (QA_PIXEL bit 7 / SCL=6 en Sentinel-2 L2A)'),
            defaultValue=False,
            optional=True
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
        fecha_inicio = self.parameterAsString(
            parameters, self.DATE_START, context).strip()
        fecha_fin = self.parameterAsString(
            parameters, self.DATE_END, context).strip()
        cloud_pct = self.parameterAsDouble(parameters, self.CLOUD_PCT, context)
        con_tirs = self.parameterAsBool(parameters, self.THERMAL, context)
        min_cov = self.parameterAsDouble(
            parameters, self.MIN_COVERAGE, context)
        export_metodo = self.parameterAsEnum(
            parameters, self.EXPORT_METHOD, context)
        gcs_bucket = self.parameterAsString(
            parameters, self.GCS_BUCKET, context).strip()
        local_dir = self.parameterAsString(parameters, self.LOCAL_DIR, context)
        csv_path = self.parameterAsFileOutput(
            parameters, self.OUTPUT_TABLE, context)
        mask_clouds = self.parameterAsBool(
            parameters, self.MASK_CLOUDS, context)
        mask_cirrus = self.parameterAsBool(
            parameters, self.MASK_CIRRUS, context)
        mask_shadows = self.parameterAsBool(
            parameters, self.MASK_SHADOWS, context)
        mask_snow_ice = self.parameterAsBool(
            parameters, self.MASK_SNOW_ICE, context)
        mask_water = self.parameterAsBool(parameters, self.MASK_WATER, context)

        mask_opts = {
            'clouds': mask_clouds,
            'cirrus': mask_cirrus,
            'shadows': mask_shadows,
            'snow_ice': mask_snow_ice,
            'water': mask_water,
        }

        info = SENSORES[idx_sensor]

        if export_metodo == 1 and not gcs_bucket:
            raise QgsProcessingException(
                "Debe especificar un Bucket de Google Cloud Storage.")
        if export_metodo == 2 and not local_dir:
            feedback.pushWarning(
                "No se especificó una Carpeta local para la Descarga Directa. Cambiando a Google Drive por defecto.")
            export_metodo = 0

        # ── Validaciones basicas ──────────────────────────────────
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

        if con_tirs and info['tipo'] == 'sentinel2':
            feedback.pushWarning(
                "AVISO: Sentinel-2 no tiene banda termica. "
                "La opcion TIRS sera ignorada."
            )
            con_tirs = False

        if con_tirs and not info['bandas_tirs']:
            feedback.pushWarning(
                f"AVISO: {info['sensor']} no tiene bandas TIRS registradas. "
                "Se exportara solo la banda SR."
            )
            con_tirs = False

        # ── Extent / Vector → WGS84 ───────────────────────────────
        vector_source = self.parameterAsSource(parameters, self.VECTOR_AOI, context)
        ext = self.parameterAsExtent(parameters, self.EXTENT, context)
        crs_geo = QgsCoordinateReferenceSystem('EPSG:4326')
        
        import json
        global ee
        
        geometries = []
        if vector_source:
            # Transform and read vector
            tr = QgsCoordinateTransform(vector_source.sourceCrs(), crs_geo, QgsProject.instance())
            for feat in vector_source.getFeatures():
                geom = feat.geometry()
                if not geom.isEmpty():
                    geom.transform(tr)
                    geometries.append(json.loads(geom.asJson()))
            
            if not geometries:
                raise QgsProcessingException("La capa vectorial no tiene geometrías válidas.")
                
            v_ext = vector_source.sourceExtent()
            pmin = tr.transform(v_ext.xMinimum(), v_ext.yMinimum())
            pmax = tr.transform(v_ext.xMaximum(), v_ext.yMaximum())
            lon_min, lat_min = pmin.x(), pmin.y()
            lon_max, lat_max = pmax.x(), pmax.y()
        elif ext and not ext.isEmpty():
            crs_ext = self.parameterAsExtentCrs(parameters, self.EXTENT, context)
            if not crs_ext.isGeographic():
                tr = QgsCoordinateTransform(crs_ext, crs_geo, QgsProject.instance())
                pmin = tr.transform(ext.xMinimum(), ext.yMinimum())
                pmax = tr.transform(ext.xMaximum(), ext.yMaximum())
                lon_min, lat_min = pmin.x(), pmin.y()
                lon_max, lat_max = pmax.x(), pmax.y()
            else:
                lon_min, lat_min = ext.xMinimum(), ext.yMinimum()
                lon_max, lat_max = ext.xMaximum(), ext.yMaximum()
        else:
            raise QgsProcessingException("Debe proporcionar una capa vectorial AOI o una extension de area.")

        # ── Resumen inicial ───────────────────────────────────────
        mascaras_activas = [
            nombre for nombre, activo in [
                ('Nubes', mask_clouds),
                ('Cirros', mask_cirrus),
                ('Sombras', mask_shadows),
                ('Nieve/Hielo', mask_snow_ice),
                ('Agua', mask_water),
            ] if activo
        ]
        feedback.pushInfo('═' * 62)
        feedback.pushInfo('  DESCARGA GEE — ' + info['label'])
        feedback.pushInfo('═' * 62)
        feedback.pushInfo(f"  Coleccion GEE  : {info['collection']}")
        feedback.pushInfo(f"  Periodo        : {fecha_inicio}  a  {fecha_fin}")
        feedback.pushInfo(f"  Nubosidad max  : {cloud_pct:.0f}%")
        feedback.pushInfo(f"  Cobertura min  : {min_cov:.0%}")
        feedback.pushInfo(f"  Banda termica  : {'Si' if con_tirs else 'No'}")
        feedback.pushInfo(
            f"  Mascaras       : "
            + (', '.join(mascaras_activas)
               if mascaras_activas else 'Ninguna (todas desactivadas)')
        )
        feedback.pushInfo(f"  Bbox WGS84     : [{lon_min:.5f}, {lat_min:.5f}, "
                          f"{lon_max:.5f}, {lat_max:.5f}]")
        feedback.pushInfo(f"  Google Drive   : {info['gdrive']}")
        if export_metodo == 0:
            feedback.pushInfo(
                f"  Destino        : Google Drive ({
                    info['gdrive']})")
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
        ee = _ensure_ee(feedback)
        _autenticar_gee(ee, feedback)

        # ── AOI y coleccion base ───────────────────────────────────
        if geometries:
            # GEE no acepta el campo "crs" que QGIS agrega al GeoJSON.
            # Lo eliminamos para que ee.Geometry lo acepte correctamente.
            def _clean_geojson(g):
                cleaned = {k: v for k, v in g.items() if k != 'crs'}
                return cleaned

            clean_geoms = [_clean_geojson(g) for g in geometries]

            if len(clean_geoms) == 1:
                aoi = ee.Geometry(clean_geoms[0])
            else:
                features = [ee.Feature(ee.Geometry(_clean_geojson(g))) for g in geometries]
                aoi = ee.FeatureCollection(features).geometry()
        else:
            aoi = ee.Geometry.Rectangle([lon_min, lat_min, lon_max, lat_max])

        is_l2a = (info['sensor'] == 'SENTINEL2_L2A')
        mask_fn = (
            _make_mask_landsat(mask_opts)
            if info['tipo'] == 'landsat'
            else _make_mask_sentinel2(mask_opts, is_l2a)
        )

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

            task_id_sr = None
            task_id_tirs = None

            # ── Exportar SR multiespectral ─────────────────────────
            img_sr = img.select(info['bandas_sr'])

            if info['tipo'] == 'landsat':
                # Factor de escala Landsat C2 L2 SR
                img_sr = img_sr.multiply(0.0000275).add(-0.2).clip(aoi)
                nombre_sr = _limpiar_nombre(
                    f"{anio}_{info['sensor']}_RS_{id_clean}_{nube_fmt}"
                )
            else:
                img_sr = img_sr.clip(aoi)
                nombre_sr = _limpiar_nombre(
                    f"{anio}_{info['sensor']}_RS_{id_clean}_{nube_fmt}"
                )

            if export_metodo == 2:  # Local
                out_path = os.path.join(local_dir, f"{nombre_sr}.tif")
                try:
                    _descarga_directa(
                        ee, img_sr, info['scale_sr'], aoi, out_path, feedback)
                    task_id_sr = 'LOCAL'
                    feedback.pushInfo(
                        f"  [{i + 1:03d}/{n_imagenes}] SR   → {nombre_sr} (Descargado)")
                except Exception as e:
                    feedback.pushWarning(
                        f"Descarga directa falló: {e}. Reintentando con Google Drive...")
                    destino = info['gdrive']
                    task_id_sr = _exportar_imagen(
                        ee, img_sr, nombre_sr, destino,
                        info['scale_sr'], aoi, 0, feedback
                    )
                    feedback.pushInfo(
                        f"  [{i + 1:03d}/{n_imagenes}] SR   → {nombre_sr}  "
                        f"(task: {task_id_sr})"
                    )
            else:
                destino = info['gdrive'] if export_metodo == 0 else gcs_bucket
                task_id_sr = _exportar_imagen(
                    ee, img_sr, nombre_sr, destino,
                    info['scale_sr'], aoi, export_metodo, feedback
                )
                feedback.pushInfo(
                    f"  [{i + 1:03d}/{n_imagenes}] SR   → {nombre_sr}  "
                    f"(task: {task_id_sr})"
                )

            # ── Exportar TIRS (opcional, solo Landsat) ─────────────
            nombre_tirs = None
            if con_tirs and info['bandas_tirs']:
                # Temperatura superficial Landsat C2 L2 → grados Celsius
                img_tirs = (
                    img.select(info['bandas_tirs'])
                    .multiply(0.00341802).add(149.0)    # escala ST a Kelvin
                    .subtract(273.15)                   # Kelvin → Celsius
                    .clip(aoi)
                )
                nombre_tirs = _limpiar_nombre(
                    f"{anio}_{info['sensor']}_TIRS_{id_clean}_{nube_fmt}"
                )
                if export_metodo == 2:
                    out_tirs = os.path.join(local_dir, f"{nombre_tirs}.tif")
                    try:
                        _descarga_directa(
                            ee, img_tirs, info['scale_tirs'], aoi, out_tirs, feedback)
                        task_id_tirs = 'LOCAL'
                        feedback.pushInfo(
                            f"  [{i + 1:03d}/{n_imagenes}] TIRS → {nombre_tirs} (Descargado)")
                    except Exception as e:
                        feedback.pushWarning(
                            f"Descarga directa falló: {e}. Reintentando con Google Drive...")
                        destino_tirs = info['gdrive']
                        task_id_tirs = _exportar_imagen(
                            ee, img_tirs, nombre_tirs, destino_tirs,
                            info['scale_tirs'], aoi, 0, feedback
                        )
                        feedback.pushInfo(
                            f"  [{i + 1:03d}/{n_imagenes}] TIRS → {nombre_tirs}  "
                            f"(task: {task_id_tirs})"
                        )
                else:
                    destino_tirs = info['gdrive'] if export_metodo == 0 else gcs_bucket
                    task_id_tirs = _exportar_imagen(
                        ee, img_tirs, nombre_tirs, destino_tirs,
                        info['scale_tirs'], aoi, export_metodo, feedback
                    )
                    feedback.pushInfo(
                        f"  [{i + 1:03d}/{n_imagenes}] TIRS → {nombre_tirs}  "
                        f"(task: {task_id_tirs})"
                    )

            # ── Registro ──────────────────────────────────────────
            dest_str = "Local" if export_metodo == 2 else (
                "GCS" if export_metodo == 1 else info['gdrive'])
            registros.append({
                'N': i + 1,
                'ID_PRODUCTO': id_producto,
                'SENSOR': info['sensor'],
                'FECHA': fecha,
                'ANIO': anio,
                'NUBOSIDAD': nube_fmt,
                'NOMBRE_SR': nombre_sr,
                'NOMBRE_TIRS': nombre_tirs or '',
                'TASK_ID_SR': task_id_sr or '',
                'TASK_ID_TIRS': task_id_tirs or '',
                'DESTINO': dest_str,
            })

            feedback.setProgress(int((i + 1) * 100 / n_imagenes))

        # ── Guardar tabla CSV ─────────────────────────────────────
        if csv_path and csv_path != 'TEMPORARY_OUTPUT':
            try:
                campos = [
                    'N', 'ID_PRODUCTO', 'SENSOR', 'FECHA', 'ANIO',
                    'NUBOSIDAD', 'NOMBRE_SR', 'NOMBRE_TIRS',
                    'TASK_ID_SR', 'TASK_ID_TIRS', 'DESTINO'
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
        feedback.pushInfo('  EXPORTACION INICIADA EXITOSAMENTE')
        feedback.pushInfo('═' * 62)
        feedback.pushInfo(f"  Sensor         : {info['label']}")
        feedback.pushInfo(f"  Periodo        : {fecha_inicio} → {fecha_fin}")
        feedback.pushInfo(f"  Imagenes SR    : {n_imagenes}")
        if con_tirs:
            feedback.pushInfo(f"  Imagenes TIRS  : {n_imagenes}")
        feedback.pushInfo(f"  Carpeta Drive  : {info['gdrive']}")
        feedback.pushInfo('')
        feedback.pushInfo('  Monitorea el progreso en:')
        feedback.pushInfo('    https://code.earthengine.google.com/tasks')
        feedback.pushInfo('')
        feedback.pushInfo('  Para descargar las imagenes:')
        feedback.pushInfo('  1. Ve a https://drive.google.com')
        feedback.pushInfo(f"  2. Abre la carpeta '{info['gdrive']}'")
        feedback.pushInfo('  3. Descarga los GeoTIFF (.tif)')
        feedback.pushInfo('  4. Arrastralos al proyecto QGIS')
        feedback.pushInfo('═' * 62)

        return {self.OUTPUT_TABLE: csv_path}

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
