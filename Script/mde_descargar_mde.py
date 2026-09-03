from ._qt_compat import qt_exec
from .geomaticape_algorithm import GeomaticapeAlgorithm
# -*- coding: utf-8 -*-
"""
mde_descargar_mde.py
Descarga MDE desde Google Earth Engine.

Estrategia dual automatica:
  - Area pequena: descarga directa getDownloadURL -> GeoTIFF local
  - Area grande : Export.toDrive -> carpeta GEE_Geomatica en Google Drive
                  con monitoreo en tiempo real del task

Limite real de GEE: 50331648 bytes (~48 MB) por request de descarga directa.
Equivale a ~25 MP para Int16 o ~12 MP para Float32.
Umbral conservador usado: 10 MP para garantizar compatibilidad con todos los tipos.

Geomaticape v1.24 - Geomatica Ambiental
"""
from qgis.core import (
    QgsProcessingException,
    QgsProcessingParameterExtent,
    QgsProcessingParameterEnum,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessing,
    QgsProject,
    QgsRasterLayer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsRectangle
)
import time
import math

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────
# GEE rechaza getDownloadURL si el payload supera 50331648 bytes (~48 MB).
# SRTM/NASADEM = Int16 = 2 bytes/px  -> limite real ~25 MP
# Copernicus/ALOS = Float32 = 4 bytes/px -> limite real ~12 MP
# Usamos 10 MP como umbral conservador que funciona con todos los tipos de
# dato.
LIMITE_DIRECTO_MP = 10_000_000   # pixeles — umbral para conmutar a Drive
GDRIVE_FOLDER = 'GEE_Geomatica'
POLL_INTERVAL_S = 12           # segundos entre consultas de estado

# ─────────────────────────────────────────────────────────────────────────────
# Catalogo de DEMs
# ─────────────────────────────────────────────────────────────────────────────
GEE_DEMS = [
    {
        'label': 'SRTM GL1 — 30 m global (NASA/USGS)',
        'collection': 'USGS/SRTMGL1_003',
        'band': 'elevation',
        'scale': 30,
        'tipo': 'Image',
        'dtype': 'Int16',
        'desc': 'SRTM 1 arc-sec. Global 56S-60N. Datum EGM96.',
    },
    {
        'label': 'Copernicus DEM GLO-30 — 30 m global',
        'collection': 'COPERNICUS/DEM/GLO30',
        'band': 'DEM',
        'scale': 30,
        'tipo': 'ImageCollection',
        'dtype': 'Float32',
        'desc': 'TanDEM-X. Cobertura global. Alta precision.',
    },
    {
        'label': 'NASADEM — 30 m global (NASA)',
        'collection': 'NASA/NASADEM_HGT/001',
        'band': 'elevation',
        'scale': 30,
        'tipo': 'Image',
        'dtype': 'Int16',
        'desc': 'SRTM reprocesado con ASTER/ICESat. Global.',
    },
    {
        'label': 'ALOS AW3D30 — 30 m global (JAXA)',
        'collection': 'JAXA/ALOS/AW3D30/V3_2',
        'band': 'DSM',
        'scale': 30,
        'tipo': 'ImageCollection',
        'dtype': 'Float32',
        'desc': 'DSM global de ALOS PRISM. JAXA.',
    },
    {
        'label': 'MERIT DEM — 90 m global (corregido)',
        'collection': 'MERIT/DEM/v1_0_3',
        'band': 'dem',
        'scale': 90,
        'tipo': 'Image',
        'dtype': 'Float32',
        'desc': 'SRTM/AW3D30 corregido por vegetacion y speckle.',
    },
    {
        'label': 'SRTM 90 m (CGIAR-CSI v4)',
        'collection': 'CGIAR/SRTM90_V4',
        'band': 'elevation',
        'scale': 90,
        'tipo': 'Image',
        'dtype': 'Int16',
        'desc': 'SRTM 90 m con vacios rellenados. CGIAR-CSI v4.',
    },
    {
        'label': 'HydroSHEDS — 90 m void-filled (WWF)',
        'collection': 'WWF/HydroSHEDS/03VFDEM',
        'band': 'b1',
        'scale': 90,
        'tipo': 'Image',
        'dtype': 'Int16',
        'desc': 'DEM hidrologicamente acondicionado. Tropicos/Subtropicos.',
    },
    {
        'label': 'ASTER GDEM v3 — 30 m global (NASA/METI)',
        'collection': 'NASA/ASTER_GED/AG100_003',
        'band': 'elevation',
        'scale': 30,
        'tipo': 'Image',
        'dtype': 'Float32',
        'desc': 'ASTER GDEM v3. Cobertura 83N-83S.',
    },
    {
        'label': '3DEP 1 m (solo EE.UU.) — USGS',
        'collection': 'USGS/3DEP/1m',
        'band': 'elevation',
        'scale': 1,
        'tipo': 'ImageCollection',
        'dtype': 'Float32',
        'desc': '3D Elevation Program 1 metro. Solo EE.UU.',
    },
]

DEM_LABELS = [d['label'] for d in GEE_DEMS]

# Bytes por pixel segun dtype
DTYPE_BYTES = {'Int16': 2, 'Int32': 4, 'Float32': 4, 'Float64': 8, 'Byte': 1}


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


def _construir_imagen(ee, dem_info, region):
    cid = dem_info['collection']
    band = dem_info['band']
    if dem_info['tipo'] == 'Image':
        img = ee.Image(cid).select(band)
    else:
        img = (ee.ImageCollection(cid)
                 .filterBounds(region)
                 .mosaic()
                 .select(band))
    return img.clip(region)


def _estimar_pixeles(lon_min, lat_min, lon_max, lat_max, scale):
    """Pixeles con correccion coseno latitudinal."""
    lat_med = (lat_min + lat_max) / 2.0
    cos_lat = math.cos(math.radians(lat_med))
    ancho_m = (lon_max - lon_min) * 111320.0 * cos_lat
    alto_m = (lat_max - lat_min) * 111320.0
    px_x = max(1, int(ancho_m / scale))
    px_y = max(1, int(alto_m / scale))
    return px_x * px_y, px_x, px_y


def _estimar_bytes(total_px, dtype):
    """Peso estimado del raster en bytes."""
    bpp = DTYPE_BYTES.get(dtype, 4)
    return total_px * bpp


def _descarga_directa(ee, imagen, scale, region, output_path, feedback, nodata_val=None):
    """getDownloadURL -> GeoTIFF local. Maneja ZIP o TIF directo y restaura nombres de bandas."""
    import zipfile
    import os
    import shutil
    from urllib.parse import urlparse

    feedback.pushInfo(
        f"Generando URL de descarga directa en GEE para {os.path.basename(output_path)}...")

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
                "Reduce el área, aumenta la escala, o selecciona Export-to-Drive.\n\n"
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

    # ── Procesar archivo descargado ─────────────────────────────
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

    # Restaurar nombres de bandas internos y asignar nodata usando GDAL
    if band_names or nodata_val is not None:
        try:
            from osgeo import gdal
            ds = gdal.Open(output_path, gdal.GA_Update)
            if ds:
                for i in range(ds.RasterCount):
                    band = ds.GetRasterBand(i + 1)
                    if band_names and i < len(band_names):
                        band.SetDescription(band_names[i])
                    if nodata_val is not None:
                        band.SetNoDataValue(float(nodata_val))
                ds = None
        except Exception as e:
            feedback.pushWarning(
                f"No se pudieron renombrar las bandas internas: {e}")

    feedback.pushInfo(f"MDE guardado en: {output_path}")


def _exportar_drive(ee, imagen, scale, region, task_name, feedback):
    """
    Export.image.toDrive + monitoreo en tiempo real.
    La carpeta GEE_Geomatica se crea automaticamente si no existe.
    """
    feedback.pushInfo("=" * 58)
    feedback.pushInfo("  AREA GRANDE: exportando a Google Drive")
    feedback.pushInfo(f"  Carpeta : {GDRIVE_FOLDER}")
    feedback.pushInfo(f"  Archivo : {task_name}.tif")
    feedback.pushInfo("=" * 58)

    task = ee.batch.Export.image.toDrive(
        image=imagen,
        description=task_name,
        folder=GDRIVE_FOLDER,
        fileNamePrefix=task_name,
        scale=scale,
        region=region,
        crs='EPSG:4326',
        fileFormat='GeoTIFF',
        maxPixels=1e13,
    )
    task.start()

    feedback.pushInfo(f"Task GEE iniciado. ID: {task.id}")
    feedback.pushInfo(
        "Monitorea el progreso en:\n"
        "  https://code.earthengine.google.com/tasks"
    )
    feedback.pushInfo("Esperando que GEE complete la exportacion...")

    ESTADOS_FIN = {'COMPLETED', 'FAILED', 'CANCELLED', 'CANCEL_REQUESTED'}
    ultimo_estado = ''
    espera_total = 0
    spinner = ['|', '/', '-', '\\']
    spin_idx = 0

    while True:
        time.sleep(POLL_INTERVAL_S)
        espera_total += POLL_INTERVAL_S

        if feedback.isCanceled():
            feedback.pushInfo("Cancelando task en GEE...")
            task.cancel()
            raise QgsProcessingException("Proceso cancelado por el usuario.")

        try:
            status = task.status()
            estado = status.get('state', 'UNKNOWN')
            progreso = status.get('progress', 0.0)
        except Exception as ex:
            feedback.pushWarning(
                f"No se pudo consultar el estado del task: {ex}")
            continue

        if estado != ultimo_estado:
            feedback.pushInfo(f"  Estado GEE : {estado}")
            ultimo_estado = estado

        mins = espera_total // 60
        segs = espera_total % 60
        spin = spinner[spin_idx % len(spinner)]
        spin_idx += 1

        if progreso and progreso > 0:
            feedback.setProgress(int(progreso * 100))
            feedback.pushInfo(
                f"  {spin} Progreso: {progreso * 100:.1f}%  |  "
                f"Tiempo transcurrido: {mins}m {segs}s"
            )
        else:
            feedback.pushInfo(
                f"  {spin} Procesando en GEE...  |  "
                f"Tiempo transcurrido: {mins}m {segs}s"
            )

        if estado in ESTADOS_FIN:
            break

    if estado == 'COMPLETED':
        feedback.pushInfo("=" * 58)
        feedback.pushInfo("  Exportacion COMPLETADA exitosamente.")
        feedback.pushInfo(f"  Archivo: {task_name}.tif")
        feedback.pushInfo(f"  Carpeta Google Drive: {GDRIVE_FOLDER}")
        feedback.pushInfo("")
        feedback.pushInfo("  Para usarlo en QGIS:")
        feedback.pushInfo("  1. Abre https://drive.google.com")
        feedback.pushInfo(f"  2. Ve a la carpeta '{GDRIVE_FOLDER}'")
        feedback.pushInfo(f"  3. Descarga '{task_name}.tif'")
        feedback.pushInfo("  4. Arrastralo al proyecto QGIS o usa")
        feedback.pushInfo("     Capa > Anadir capa raster.")
        feedback.pushInfo("=" * 58)
        return True

    error_msg = task.status().get('error_message', 'Error desconocido')
    raise QgsProcessingException(
        f"La exportacion a Google Drive fallo.\n"
        f"Estado: {estado}\n"
        f"Detalle: {error_msg}\n\n"
        f"Revisa el panel de tasks:\n"
        f"  https://code.earthengine.google.com/tasks"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Algoritmo QGIS Processing
# ─────────────────────────────────────────────────────────────────────────────

class MDEDescargarMDE(GeomaticapeAlgorithm):
    _algorithm_name = "mde_descargar_mde"
    _icon_name = "extraer_valores.png"

    EXTENT = 'EXTENT'
    INPUT_LAYER = 'INPUT_LAYER'
    BUFFER_DIST = 'BUFFER_DIST'
    CLIP_GEOM = 'CLIP_GEOM'
    NAME_FIELD = 'NAME_FIELD'
    DATASET = 'DATASET'
    SCALE = 'SCALE'
    OPEN = 'OPEN'
    OUTPUT_DIR = 'OUTPUT_DIR'

    def displayName(self):
        return self.tr('Descargar MDE')

    def group(self):
        return self.tr('MDE')

    def groupId(self):
        return 'mde_geo'

    def tags(self):
        return ['mde', 'dem', 'elevacion', 'srtm', 'copernicus', 'nasadem',
                'alos', 'merit', 'aster', '3dep', 'gee', 'google earth engine',
                'google drive', 'descarga', 'modelo', 'terreno', 'digital']

    def shortHelpString(self):
        catalogo = ''.join(
            f'<li><b>{d["label"]}</b><br>'
            f'<small>{d["desc"]} | Nativo: {d["scale"]} m | Tipo: {d["dtype"]}</small></li>'
            for d in GEE_DEMS
        )
        limite_mp = LIMITE_DIRECTO_MP // 1_000_000
        return f"""
<b>Descargar MDE desde Google Earth Engine</b><br><br>

La herramienta selecciona automaticamente el metodo de descarga:<br><br>

<table border="1" cellpadding="4" style="border-collapse:collapse;">
<tr style="background:#ddd"><th>Condicion</th><th>Metodo</th></tr>
<tr><td>Area &le; {limite_mp} MP</td>
    <td><b>Descarga directa</b> → GeoTIFF al disco local</td></tr>
<tr><td>Area &gt; {limite_mp} MP</td>
    <td><b>Google Drive</b> → carpeta <code>{GDRIVE_FOLDER}</code><br>
        <small>Monitoreo en tiempo real. Luego descarga desde drive.google.com</small></td></tr>
</table><br>

<b>Limite tecnico de GEE:</b> getDownloadURL acepta hasta 48 MB por request
(~25 MP en Int16, ~12 MP en Float32). El umbral de {limite_mp} MP garantiza
compatibilidad con todos los tipos de dato.<br><br>

<b>Opciones de Área de Interés:</b><br>
Puedes descargar usando una extensión (bounding box) simple, o bien usando una <b>capa vectorial</b>.
Si usas una capa vectorial, puedes definir un buffer y elegir si se debe recortar la imagen a la geometría.
Si la capa tiene varios polígonos, se descargará un MDE por cada polígono.<br><br>

<b>Configuracion inicial (una sola vez):</b><br>
<ol>
  <li>Cuenta aprobada en <a href="https://earthengine.google.com">earthengine.google.com</a></li>
  <li><code>python -m pip install earthengine-api</code></li>
  <li>En la consola Python de QGIS:<br>
      <code>import ee</code><br>
      <code>ee.Authenticate()</code><br>
      <code>ee.Initialize(project='mi-proyecto-gee')</code>
  </li>
</ol>

<b>DEMs disponibles:</b><br><ul>{catalogo}</ul>
"""

    def initAlgorithm(self, config=None):
        from qgis.core import (
            QgsProcessingParameterExtent,
            QgsProcessingParameterEnum,
            QgsProcessingParameterBoolean,
            QgsProcessingParameterFolderDestination,
            QgsProcessingParameterNumber,
            QgsProcessingParameterFeatureSource,
            QgsProcessingParameterField,
            QgsProcessing
        )
        
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT,
            self.tr('Extension del area de interes (Opcional si usa vector)'),
            optional=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT_LAYER,
            self.tr('Capa vectorial de poligonos (Opcional, prioriza sobre extension)'),
            types=[QgsProcessing.TypeVectorPolygon],
            optional=True
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.BUFFER_DIST,
            self.tr('Distancia de buffer (metros)'),
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=0.0,
            optional=True
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.CLIP_GEOM,
            self.tr('Recortar exactamente por la geometria del vector/buffer'),
            defaultValue=False,
            optional=True
        ))
        self.addParameter(QgsProcessingParameterField(
            self.NAME_FIELD,
            self.tr('Campo para nombrar archivos (Opcional, solo si usa capa vectorial)'),
            parentLayerParameterName=self.INPUT_LAYER,
            optional=True
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.DATASET,
            self.tr('Fuente de elevacion (DEM)'),
            options=DEM_LABELS,
            defaultValue=0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.SCALE,
            self.tr('Escala de salida (m)  —  0 = resolucion nativa del DEM'),
            type=QgsProcessingParameterNumber.Type.Integer,
            defaultValue=0,
            minValue=0,
            optional=True
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.OPEN,
            self.tr('Cargar MDE en QGIS al finalizar (solo descarga directa)'),
            defaultValue=True
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_DIR,
            self.tr('Directorio de salida para los archivos descargados')
        ))

    def processAlgorithm(self, parameters, context, feedback):
        import json
        import os
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsProject,
            QgsGeometry,
            QgsRectangle,
            QgsProcessingException
        )

        idx_dem = self.parameterAsEnum(parameters, self.DATASET, context)
        escala = self.parameterAsInt(parameters, self.SCALE, context)
        output_dir = self.parameterAsString(parameters, self.OUTPUT_DIR, context)
        carregar = self.parameterAsBool(parameters, self.OPEN, context)

        ext = self.parameterAsExtent(parameters, self.EXTENT, context)
        input_layer = self.parameterAsSource(parameters, self.INPUT_LAYER, context)
        buffer_dist = self.parameterAsDouble(parameters, self.BUFFER_DIST, context)
        clip_geom = self.parameterAsBool(parameters, self.CLIP_GEOM, context)
        name_field = self.parameterAsString(parameters, self.NAME_FIELD, context)

        if not input_layer and not ext:
            raise QgsProcessingException("Debe proporcionar una Extensión o una Capa Vectorial.")

        if not output_dir:
            raise QgsProcessingException("Debe proporcionar un Directorio de salida válido.")

        os.makedirs(output_dir, exist_ok=True)

        dem_info = GEE_DEMS[idx_dem]
        scale_eff = escala if escala > 0 else dem_info['scale']

        # ── Autenticar GEE ────────────────────────────────────────
        ee = _ensure_ee(feedback)
        _autenticar_gee(ee, feedback)

        # ── Preparar geometrías ───────────────────────────────────
        features_to_process = []
        crs_geo = QgsCoordinateReferenceSystem('EPSG:4326')

        if input_layer:
            crs_src = input_layer.sourceCrs()
            transform = QgsCoordinateTransform(crs_src, crs_geo, QgsProject.instance())
            
            field_idx = -1
            if name_field:
                field_idx = input_layer.fields().lookupField(name_field)
            
            for i, feat in enumerate(input_layer.getFeatures()):
                if feedback.isCanceled():
                    break
                
                geom = feat.geometry()
                if not geom.isNull():
                    geom.transform(transform)
                    
                    name = f"poligono_{i+1}"
                    if field_idx != -1:
                        val = feat.attributes()[field_idx]
                        if val is not None:
                            name = str(val).replace(' ', '_').replace('/', '_')
                        
                    features_to_process.append({
                        'name': name,
                        'geom_4326': geom
                    })
        else:
            crs_ext = self.parameterAsExtentCrs(parameters, self.EXTENT, context)
            if not crs_ext.isGeographic():
                transform = QgsCoordinateTransform(crs_ext, crs_geo, QgsProject.instance())
                pmin = transform.transform(ext.xMinimum(), ext.yMinimum())
                pmax = transform.transform(ext.xMaximum(), ext.yMaximum())
                lon_min, lat_min = pmin.x(), pmin.y()
                lon_max, lat_max = pmax.x(), pmax.y()
            else:
                lon_min, lat_min = ext.xMinimum(), ext.yMinimum()
                lon_max, lat_max = ext.xMaximum(), ext.yMaximum()
            
            geom = QgsGeometry.fromRect(QgsRectangle(lon_min, lat_min, lon_max, lat_max))
            features_to_process.append({
                'name': 'extension',
                'geom_4326': geom
            })

        if not features_to_process:
            raise QgsProcessingException("No se encontraron geometrías válidas para procesar.")

        feedback.pushInfo('─' * 58)
        feedback.pushInfo(f"DEM             : {dem_info['label']}")
        feedback.pushInfo(f"Coleccion GEE   : {dem_info['collection']}")
        feedback.pushInfo(f"Banda / Tipo    : {dem_info['band']} / {dem_info['dtype']}")
        feedback.pushInfo(f"Escala efectiva : {scale_eff} m")
        feedback.pushInfo(f"Buffer          : {buffer_dist} m")
        feedback.pushInfo(f"Recorte exacto  : {'Si' if clip_geom else 'No'}")
        feedback.pushInfo(f"Total a procesar: {len(features_to_process)} polígonos")
        feedback.pushInfo('─' * 58)

        self._descargados = []
        self._via_drive = False
        self._carregar = carregar
        self._dem_name = dem_info['label']

        for idx, item in enumerate(features_to_process):
            if feedback.isCanceled():
                break
                
            name = item['name']
            geom = item['geom_4326']
            
            feedback.pushInfo(f"\n--- Procesando [{idx+1}/{len(features_to_process)}]: {name} ---")
            
            try:
                geojson = json.loads(geom.asJson())
                ee_geom = ee.Geometry(geojson)
            except Exception as e:
                feedback.pushWarning(f"Error al crear geometría EE para {name}: {e}")
                continue
                
            if buffer_dist > 0:
                ee_geom = ee_geom.buffer(buffer_dist)
                
            ee_bounds = ee_geom.bounds()
            
            try:
                coords_info = ee_bounds.coordinates().getInfo()[0]
                lons = [c[0] for c in coords_info]
                lats = [c[1] for c in coords_info]
                lon_min, lon_max = min(lons), max(lons)
                lat_min, lat_max = min(lats), max(lats)
            except Exception as e:
                feedback.pushWarning(f"Error al obtener bounds para {name}: {e}")
                continue
            
            total_px, px_x, px_y = _estimar_pixeles(
                lon_min, lat_min, lon_max, lat_max, scale_eff)
            peso_mb = _estimar_bytes(total_px, dem_info['dtype']) / 1_048_576
            
            feedback.pushInfo(f"Dimension aprox : {px_y} x {px_x} px (~{peso_mb:.1f} MB)")
            
            usar_drive = total_px > LIMITE_DIRECTO_MP
            
            region_clip = ee_geom if clip_geom else ee_bounds
            imagen = _construir_imagen(ee, dem_info, region_clip)
            
            dtype_ee = dem_info['dtype']
            nodata_val = 0  # Usamos 0 como valor NoData universal para áreas enmascaradas
            
            # Enmascarar valores menores a 1 (0 o negativos)
            imagen = imagen.updateMask(imagen.gte(1))
            
            # Asignar explícitamente el valor nodata (0) a todos los píxeles enmascarados
            imagen = imagen.unmask(nodata_val)
            
            if usar_drive:
                feedback.pushInfo("AVISO: Tamaño excede límite directo (48MB). Enviando a Google Drive.")
                import datetime
                ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
                dem_slug = dem_info['collection'].replace('/', '_')
                task_name = f"MDE_{name}_{dem_slug}_{ts}"
                
                _exportar_drive(ee, imagen, scale_eff, ee_bounds, task_name, feedback)
                self._via_drive = True
            else:
                out_path = os.path.join(output_dir, f"MDE_{name}.tif")
                _descarga_directa(ee, imagen, scale_eff, ee_bounds, out_path, feedback, nodata_val)
                self._descargados.append(out_path)

        if not self._via_drive and not self._descargados:
            raise QgsProcessingException("No se pudo descargar ninguna imagen.")

        return {self.OUTPUT_DIR: output_dir}

    def postProcessAlgorithm(self, context, feedback):
        from qgis.core import QgsRasterLayer, QgsProject
        
        if getattr(self, '_carregar', False) and hasattr(self, '_descargados'):
            nombre = self._dem_name.split('—')[0].strip()
            for path in self._descargados:
                base_name = __import__('os').path.basename(path).replace('.tif', '')
                rlayer = QgsRasterLayer(path, f'{base_name} - {nombre}')
                if rlayer.isValid():
                    QgsProject.instance().addMapLayer(rlayer)
                else:
                    feedback.pushWarning(f'El raster se descargó pero no pudo cargarse: {path}')
        return {}

    def run(self):
        from qgis import processing
        processing.execAlgorithmDialog(self)
