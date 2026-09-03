from .geomaticape_algorithm import GeomaticapeAlgorithm
from .indices_espectrales import INDICES, INDEX_KEYS
import os
import gc
import numpy as np
from qgis.core import (
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterEnum,
    QgsProcessingParameterBand,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFolderDestination,
    QgsProcessingException
)
from qgis import processing
from osgeo import gdal

"""
Indices espectrales - Seleccion multiple
=========================================
Variante por lotes de "Indices espectrales". En vez de calcular un solo
indice y entregar una capa, el usuario marca cuantos indices quiera del
mismo catalogo (NDVI, SAVI, EVI, NDWI, NBR, NDSI, etc.), asigna una sola
vez las bandas del raster (BLUE/GREEN/RED/REDEDGE/NIR/SWIR1/SWIR2), y la
herramienta genera un GeoTIFF por cada indice seleccionado dentro de una
carpeta de salida.

Reutiliza el mismo catalogo INDICES (misma formula, mismos requisitos de
banda) definido en indices_espectrales.py para no duplicar logica ni
arriesgar que ambas herramientas queden desincronizadas.

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape
Grupo : Procesamiento
"""


def _safe_filename(name):
    """Limpia un texto para usarlo como parte de un nombre de archivo."""
    nm = "".join(c if c.isalnum() or c in "_-" else "_" for c in str(name))
    return nm.strip("_") or "raster"


class IndicesEspectralesSeleccion(GeomaticapeAlgorithm):
    _algorithm_name = "indices_espectrales_seleccion"
    _icon_name = "indices.png"
    """
    Calcula VARIOS indices espectrales a la vez (seleccion multiple) a
    partir de una misma imagen multiespectral y los guarda como GeoTIFF
    independientes en una carpeta de salida.
    """

    INPUT_RASTER = "INPUT_RASTER"
    INDEX_TYPES = "INDEX_TYPES"
    PREFIX = "PREFIX"
    GENERATE_REPORT = "GENERATE_REPORT"
    OUT_FOLDER = "OUT_FOLDER"

    # Parametros de banda por rol (identicos a IndicesEspectrales)
    BAND_BLUE = "BAND_BLUE"
    BAND_GREEN = "BAND_GREEN"
    BAND_RED = "BAND_RED"
    BAND_REDEDGE = "BAND_REDEDGE"
    BAND_NIR = "BAND_NIR"
    BAND_SWIR1 = "BAND_SWIR1"
    BAND_SWIR2 = "BAND_SWIR2"

    ROLE_PARAM = {
        "BLUE": BAND_BLUE,
        "GREEN": BAND_GREEN,
        "RED": BAND_RED,
        "REDEDGE": BAND_REDEDGE,
        "NIR": BAND_NIR,
        "SWIR1": BAND_SWIR1,
        "SWIR2": BAND_SWIR2,
    }

    # -------------------------------------------------------
    # IDENTIFICACION
    # -------------------------------------------------------

    def displayName(self):
        return self.tr(
            "Indices espectrales - Seleccion multiple (NDVI, SAVI, EVI, NDWI, NBR, NDSI...)")

    def group(self):
        return self.tr("Procesamiento")

    def groupId(self):
        return "geomaticape_procesamiento"

    def shortHelpString(self):
        rows = []
        for k, v in INDICES.items():
            rows.append("<li><b>{0}</b>: {1}<br/>"
                        "&nbsp;&nbsp;Bandas: {2}<br/>"
                        "&nbsp;&nbsp;Formula: <code>{3}</code></li>"
                        .format(k, v["name"], ", ".join(v["bands"]), v["formula"]))
        catalogo = "\n".join(rows)

        return f"""
<h3>Indices espectrales - Seleccion multiple</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br><br>

<b>Descripcion:</b><br>
Calcula varios indices espectrales en un solo proceso a partir de una
imagen multiespectral de cualquier satelite (Landsat, Sentinel-2,
CBERS-04A, PlanetScope, RapidEye, etc.). Se asignan las bandas del
raster una sola vez (solo las que realmente requieran los indices
marcados) y se genera un GeoTIFF independiente por cada indice
seleccionado, dentro de la carpeta de salida indicada.

<b>Comportamiento ante bandas faltantes:</b><br>
Si un indice marcado requiere una banda que no fue asignada, ese indice
se omite (no detiene el proceso) y se reporta en el log y en el reporte
de texto; el resto de indices marcados sí se calculan.

<b>Ejemplo Landsat 8 SR (6 bandas: B2..B7):</b><br>
B1=Blue, B2=Green, B3=Red, B4=NIR, B5=SWIR1, B6=SWIR2

<b>Ejemplo Sentinel-2 L2A:</b> Blue=B2, Green=B3, Red=B4, RedEdge=B5,
NIR=B8, SWIR1=B11, SWIR2=B12

<b>Indices disponibles ({len(INDICES)}):</b>
<ul>
{catalogo}
</ul>

<b>Salida:</b> un GeoTIFF de 1 banda (Float32, LZW) por cada indice
seleccionado, con nombre <code>&lt;prefijo&gt;_&lt;INDICE&gt;.tif</code>
dentro de la carpeta elegida. Conserva georreferencia y CRS del raster
de entrada. Opcionalmente, un reporte .txt con estadisticas (min, max,
media, desviacion) de cada indice generado.

<b>Web:</b> https://www.geomatica.pe/
"""

    # -------------------------------------------------------
    # PARAMETROS
    # -------------------------------------------------------

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_RASTER,
                self.tr("Imagen multiespectral de entrada")
            )
        )

        # Seleccion multiple de indices (checklist)
        labels = [f"{k}  -  {INDICES[k]['name']}" for k in INDEX_KEYS]
        self.addParameter(
            QgsProcessingParameterEnum(
                self.INDEX_TYPES,
                self.tr("Indices espectrales a calcular (marca uno o varios)"),
                options=labels,
                defaultValue=[0],  # NDVI marcado por defecto
                allowMultiple=True
            )
        )

        # Banda BLUE
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_BLUE,
                self.tr("Banda BLUE  (azul) - usada por: EVI, BSI, VARI"),
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda GREEN
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_GREEN,
                self.tr(
                    "Banda GREEN (verde) - usada por: NDWI, MNDWI, NDSI, GNDVI, GCI, VARI, NGRDI"),
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda RED
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_RED,
                self.tr(
                    "Banda RED (rojo) - usada por: NDVI, SAVI, MSAVI, EVI, EVI2, BSI, VARI, NGRDI"),
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda RED EDGE
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_REDEDGE,
                self.tr(
                    "Banda RED EDGE (borde rojo) - usada por: NDREI  (Sentinel-2 B5/B6/B7)"),
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda NIR
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_NIR,
                self.tr(
                    "Banda NIR (infrarrojo cercano) - usada por casi todos los indices vegetacion/agua"),
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda SWIR1
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_SWIR1,
                self.tr("Banda SWIR1 - usada por: MNDWI, NDMI, NDSI, BSI, NBR2"),
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )
        # Banda SWIR2
        self.addParameter(
            QgsProcessingParameterBand(
                self.BAND_SWIR2,
                self.tr("Banda SWIR2 - usada por: NBR, NBR2"),
                parentLayerParameterName=self.INPUT_RASTER,
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.PREFIX,
                self.tr(
                    "Prefijo de los archivos de salida (vacio = nombre del raster)"),
                defaultValue="",
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.GENERATE_REPORT,
                self.tr("Generar reporte de estadisticas (.txt)"),
                defaultValue=True
            )
        )

        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUT_FOLDER,
                self.tr("Carpeta de salida")
            )
        )

    # -------------------------------------------------------
    # PROCESO PRINCIPAL
    # -------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):

        raster_layer = self.parameterAsRasterLayer(
            parameters, self.INPUT_RASTER, context)
        if raster_layer is None:
            raise QgsProcessingException(
                "No se pudo leer el raster de entrada.")
        input_path = raster_layer.source()

        idx_positions = self.parameterAsEnums(
            parameters, self.INDEX_TYPES, context)
        if not idx_positions:
            raise QgsProcessingException(
                "Debes marcar al menos un indice espectral a calcular.")
        indices_pedidos = [INDEX_KEYS[i] for i in idx_positions]

        prefix = self.parameterAsString(
            parameters, self.PREFIX, context).strip()
        if not prefix:
            prefix = _safe_filename(
                os.path.splitext(os.path.basename(input_path))[0])
        else:
            prefix = _safe_filename(prefix)

        generar_reporte = self.parameterAsBoolean(
            parameters, self.GENERATE_REPORT, context)
        out_dir = self.parameterAsString(
            parameters, self.OUT_FOLDER, context)
        os.makedirs(out_dir, exist_ok=True)

        # Leer numero de banda asignado a cada rol (None si no se asigno)
        band_assignment = {}
        for role, pname in self.ROLE_PARAM.items():
            try:
                val = self.parameterAsInt(parameters, pname, context)
            except Exception:
                val = 0
            # En QGIS, banda 0 = no asignada para parametros optional
            band_assignment[role] = val if val and val > 0 else None

        # Separar indices calculables de los que faltan bandas
        indices_validos = []
        indices_omitidos = []  # (clave, bandas_faltantes)
        for key in indices_pedidos:
            faltantes = [r for r in INDICES[key]["bands"]
                         if band_assignment.get(r) is None]
            if faltantes:
                indices_omitidos.append((key, faltantes))
            else:
                indices_validos.append(key)

        feedback.pushInfo(
            "====================================================")
        feedback.pushInfo(
            f"Indices marcados ({len(indices_pedidos)}): {', '.join(indices_pedidos)}")
        if indices_omitidos:
            feedback.pushWarning(
                "Los siguientes indices se OMITEN por falta de bandas asignadas:")
            for key, faltantes in indices_omitidos:
                feedback.pushWarning(
                    f"  - {key}: falta(n) {', '.join(faltantes)}")
        if not indices_validos:
            raise QgsProcessingException(
                "Ninguno de los indices marcados puede calcularse: falta "
                "asignar las bandas requeridas. Revisa los parametros de banda."
            )
        feedback.pushInfo(
            f"Indices a generar ({len(indices_validos)}): {', '.join(indices_validos)}")
        feedback.pushInfo(
            "====================================================")

        # Abrir el raster con GDAL
        ds_in = gdal.Open(input_path, gdal.GA_ReadOnly)
        if ds_in is None:
            raise QgsProcessingException("GDAL no pudo abrir la imagen.")

        cols = ds_in.RasterXSize
        rows = ds_in.RasterYSize
        nbands = ds_in.RasterCount
        gt = ds_in.GetGeoTransform()
        proj = ds_in.GetProjection()

        feedback.pushInfo(f"Imagen     : {os.path.basename(input_path)}")
        feedback.pushInfo(f"Dimensiones: {cols} x {rows} px, {nbands} bandas")
        feedback.pushInfo(
            f"Resolucion : {abs(gt[1]):.2f} x {abs(gt[5]):.2f} m")

        # Roles realmente necesarios (union de todos los indices validos)
        roles_necesarios = sorted({
            r for key in indices_validos for r in INDICES[key]["bands"]
        })

        # Verificar que los numeros de banda asignados existen en el raster
        for role in roles_necesarios:
            n = band_assignment[role]
            if n < 1 or n > nbands:
                raise QgsProcessingException(
                    f"La banda asignada al rol {role} (#{n}) no existe en la "
                    f"imagen (la imagen tiene {nbands} banda(s))."
                )

        feedback.setProgress(5)

        # ---------------------------------------------------
        # LEER CADA BANDA UNA SOLA VEZ (aunque la usen varios indices)
        # ---------------------------------------------------
        feedback.pushInfo("Leyendo bandas requeridas...")
        bandas = {}
        for role in roles_necesarios:
            if feedback.isCanceled():
                break
            n = band_assignment[role]
            band = ds_in.GetRasterBand(n)
            arr = band.ReadAsArray().astype(np.float64)
            nd = band.GetNoDataValue()
            if nd is not None:
                arr[arr == nd] = np.nan
            bandas[role] = arr
            feedback.pushInfo(
                f"  {role:<8} <- banda #{n} ({band.GetDescription() or 'sin nombre'})")

        ds_in = None
        feedback.setProgress(15)

        # ---------------------------------------------------
        # CALCULAR Y EXPORTAR CADA INDICE
        # ---------------------------------------------------
        driver = gdal.GetDriverByName("GTiff")
        nodata_out = -9999.0
        resumen = []  # dicts para el reporte
        n_total = len(indices_validos)

        for i, key in enumerate(indices_validos, 1):
            if feedback.isCanceled():
                break

            index_def = INDICES[key]
            feedback.pushInfo(f"[{i}/{n_total}] Calculando {key}...")

            with np.errstate(divide="ignore", invalid="ignore"):
                result = index_def["func"](bandas)
            result = np.where(np.isfinite(result), result, np.nan)

            valid = result[np.isfinite(result)]
            stats = {
                "key": key,
                "name": index_def["name"],
                "formula": index_def["formula"],
                "bands": index_def["bands"],
                "band_numbers": {r: band_assignment[r] for r in index_def["bands"]},
                "min": float(valid.min()) if valid.size else None,
                "max": float(valid.max()) if valid.size else None,
                "mean": float(valid.mean()) if valid.size else None,
                "std": float(valid.std()) if valid.size else None,
                "n_validos": int(valid.size),
                "n_total": int(result.size),
            }
            resumen.append(stats)

            if valid.size > 0:
                feedback.pushInfo(
                    f"  min={stats['min']:.4f}  max={stats['max']:.4f}  "
                    f"mean={stats['mean']:.4f}  std={stats['std']:.4f}")
            else:
                feedback.pushWarning(f"  {key}: sin pixeles validos.")

            out_arr = np.where(
                np.isnan(result), nodata_out, result).astype(np.float32)

            out_path = os.path.join(out_dir, f"{prefix}_{key}.tif")
            ds_out = driver.Create(
                out_path, cols, rows, 1, gdal.GDT_Float32,
                options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
            )
            ds_out.SetGeoTransform(gt)
            ds_out.SetProjection(proj)
            out_band = ds_out.GetRasterBand(1)
            out_band.WriteArray(out_arr)
            out_band.SetDescription(key)
            out_band.SetNoDataValue(nodata_out)
            ds_out.FlushCache()
            ds_out = None

            feedback.pushInfo(f"  -> {out_path}")

            del result, out_arr
            feedback.setProgress(15 + int(i * 80 / n_total))

        del bandas
        gc.collect()

        # ---------------------------------------------------
        # REPORTE DE TEXTO
        # ---------------------------------------------------
        if generar_reporte:
            report_path = os.path.join(
                out_dir, f"{prefix}_indices_resumen.txt")
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write("=== Indices espectrales - Seleccion multiple ===\n")
                fh.write(f"Imagen  : {input_path}\n")
                fh.write(f"Tamano  : {cols} x {rows} px, {nbands} banda(s)\n")
                fh.write(
                    f"Resol.  : {abs(gt[1]):.4f} x {abs(gt[5]):.4f}\n")
                fh.write("Asignacion de bandas usadas:\n")
                for role in roles_necesarios:
                    fh.write(f"  {role:<8}: banda #{band_assignment[role]}\n")
                fh.write(f"\n=== Indices generados ({len(resumen)}) ===\n")
                for s in resumen:
                    fh.write(f"\n[{s['key']}] {s['name']}\n")
                    fh.write(f"  Formula : {s['formula']}\n")
                    fh.write(
                        f"  Bandas  : " + ", ".join(
                            f"{r}=#{n}" for r, n in s["band_numbers"].items()) + "\n")
                    if s["min"] is not None:
                        fh.write(
                            f"  min={s['min']:.4f}  max={s['max']:.4f}  "
                            f"mean={s['mean']:.4f}  std={s['std']:.4f}  "
                            f"validos={s['n_validos']:,}/{s['n_total']:,}\n")
                    else:
                        fh.write("  Sin pixeles validos.\n")
                    fh.write(f"  Archivo : {prefix}_{s['key']}.tif\n")
                if indices_omitidos:
                    fh.write(
                        f"\n=== Indices OMITIDOS ({len(indices_omitidos)}) ===\n")
                    for key, faltantes in indices_omitidos:
                        fh.write(
                            f"  {key}: falta(n) banda(s) {', '.join(faltantes)}\n")
            feedback.pushInfo(f"Reporte guardado en: {report_path}")

        feedback.setProgress(100)
        feedback.pushInfo(
            f"COMPLETO: {len(resumen)} indice(s) generado(s) en {out_dir}")
        if indices_omitidos:
            feedback.pushInfo(
                f"({len(indices_omitidos)} indice(s) omitido(s) por falta de bandas)")

        return {self.OUT_FOLDER: out_dir}

    def run(self):
        processing.execAlgorithmDialog(self)
