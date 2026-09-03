# -*- coding: utf-8 -*-
"""
Landsat Pansharpening 30m -> 15m
=================================
Aumenta la resolucion espacial de las bandas multiespectrales de Landsat
(30 m) a 15 m fusionandolas con la banda pancromatica (15 m).

Algoritmos disponibles:
  WeightedBrovey — GDAL VRT nativo, pesos espectrales configurables.
  SimpleBrovey   — Brovey clasico (Python/NumPy): out_i = ms_i * PAN / sum(MS).
  GS             — Gram-Schmidt Spectral Fusion: inyeccion espectral optima.
  HSV            — Hue-Saturation-Value: ideal para 3 bandas RGB.
  HCS            — Hyperspherical Color Sharpening: fusion por coordenadas
                   hiperesfericas, preserva relaciones espectrales entre bandas.

UI Qt personalizada:
  Bandas multiespectrales — tabla con # / Origen / Banda / Nombre.
    "Agregar raster (archivo)..."       : banda a banda desde disco.
    "Agregar capa(s) QGIS..."           : banda a banda desde proyecto QGIS.
    "Cargar multibanda (archivo)..."    : TODAS las bandas de un raster.
    "Cargar multibanda (QGIS)..."       : TODAS las bandas de una capa QGIS.
  Banda pancromatica — archivo o capa QGIS + combo de banda.
  Salida — GeoTIFF Float32 a la resolucion del PAN (~15 m) con nombres de
           banda preservados (band.SetDescription / BAND_NAME metadata).

Notas tecnicas:
  * WeightedBrovey usa el motor VRT pansharpened de GDAL (mas rapido).
  * Los demas algoritmos usan NumPy; la imagen de salida es siempre Float32.
  * Para HSV con mas de 3 bandas, las bandas extra se fusionan con Brovey.
  * Para GS, los pesos opcionales definen el PAN sintetico de referencia.
  * HCS preserva mejor las relaciones angulares del espectro que Brovey.

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.10
Grupo : Procesamiento
"""

from ._qt_compat import qt_exec
from .geomaticape_algorithm import GeomaticapeAlgorithm
from qgis.core import QgsProcessingException, QgsMessageLog, Qgis
import os

import numpy as np

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLabel, QLineEdit,
    QMessageBox, QComboBox, QDialogButtonBox, QProgressDialog, QApplication,
)
from qgis.core import QgsProject, QgsRasterLayer, QgsMapLayerProxyModel
from qgis.gui import QgsMapLayerComboBox, QgsFileWidget
from osgeo import gdal

# Reusamos utilidades del modulo Combinar bandas (mismo paquete).
from .combinar_bandas_nombres import (
    _safe_name,
    _default_name,
    _band_count,
    _detect_band_name_at,
    _DialogFeedback,
)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_ALGO_DISPLAY_MAP = {
    "Weighted Brovey (Nativo GDAL)": "WeightedBrovey",
    "Simple Brovey": "SimpleBrovey",
    "Gram-Schmidt (GS)": "GS",
    "Hue-Saturation-Value (HSV)": "HSV",
    "Hyperspherical Color Sharpening (HCS)": "HCS",
}
PANSHARP_ALGORITHMS = list(_ALGO_DISPLAY_MAP.keys())

# Solo WeightedBrovey usa los pesos del campo de texto de forma nativa en GDAL.
# GS los usa opcionalmente para el PAN sintetico.
_WEIGHT_SUPPORTED = {"WeightedBrovey", "GS"}

# Metodo de remuestreo para subir las MS al grid del PAN
PAN_RESAMPLE = ["Cubic", "CubicSpline", "Bilinear", "Lanczos", "Average"]

_GDAL_RESAMPLE_MAP = {
    "Cubic": gdal.GRA_Cubic,
    "CubicSpline": gdal.GRA_CubicSpline,
    "Bilinear": gdal.GRA_Bilinear,
    "Lanczos": gdal.GRA_Lanczos,
    "Average": gdal.GRA_Average,
}

# Descripciones de cada algoritmo para el tooltip del combo
_ALGO_DESCRIPTIONS = {
    "WeightedBrovey": (
        "GDAL VRT nativo. Fusion rapida con pesos espectrales configurables. "
        "Recomendado para Landsat 7 ETM+ y 8/9 OLI."
    ),
    "SimpleBrovey": (
        "Brovey clasico Python/NumPy. out_i = ms_i * PAN / sum(MS). "
        "Rapido, puede alterar el balance radiometrico."
    ),
    "GS": (
        "Gram-Schmidt Spectral Fusion. Inyecta el PAN real ajustando la "
        "ganancia optima por banda. Preserva bien las firmas espectrales."
    ),
    "HSV": (
        "Hue-Saturation-Value. Reemplaza el canal de intensidad (V) con "
        "el PAN. Optimo con 3 bandas RGB; bandas extra fusionadas con Brovey."
    ),
    "HCS": (
        "Hyperspherical Color Sharpening. Convierte a coordenadas "
        "hiperesfericas y reemplaza el radio (intensidad) con el PAN. "
        "Preserva relaciones angulares entre bandas."
    ),
}


# ---------------------------------------------------------------------------
# Helpers de acceso GDAL
# ---------------------------------------------------------------------------

def _xml_escape(s):
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _open_info(path):
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL no pudo abrir: {path}")
    info = {
        "cols": ds.RasterXSize,
        "rows": ds.RasterYSize,
        "gt": ds.GetGeoTransform(),
        "proj": ds.GetProjection(),
        "nbands": ds.RasterCount,
    }
    ds = None
    return info


def _load_pan_ds(pan_path):
    """Abre el PAN y devuelve el dataset GDAL (el caller debe cerrarlo)."""
    ds = gdal.Open(pan_path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL no pudo abrir el PAN: {pan_path}")
    return ds


def _resample_band_to_pan(src_path, src_band_idx, pan_ds, resample_key):
    """
    Remuestrea una banda al grid exacto del PAN usando gdal.Warp en memoria.
    Devuelve (ndarray float64, nodata_value_or_None).
    """
    gt = pan_ds.GetGeoTransform()
    proj = pan_ds.GetProjection()
    cols = pan_ds.RasterXSize
    rows = pan_ds.RasterYSize
    xmin = gt[0]
    ymax = gt[3]
    xmax = xmin + cols * gt[1]
    ymin = ymax + rows * gt[5]

    resample_alg = _GDAL_RESAMPLE_MAP.get(resample_key, gdal.GRA_Cubic)

    mem_ds = gdal.Warp(
        '', src_path,
        format='MEM',
        srcBands=[src_band_idx],
        outputBounds=(xmin, ymin, xmax, ymax),
        width=cols, height=rows,
        dstSRS=proj or None,
        resampleAlg=resample_alg,
    )
    if mem_ds is None:
        raise RuntimeError(
            f"No se pudo remuestrear la banda {src_band_idx} de "
            f"{os.path.basename(src_path)}"
        )
    arr = mem_ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    nd = mem_ds.GetRasterBand(1).GetNoDataValue()
    mem_ds = None
    return arr, nd


# ---------------------------------------------------------------------------
# Implementaciones de algoritmos Python / NumPy
# ---------------------------------------------------------------------------

def _histogram_match_linear(src, ref):
    """
    Ajuste lineal de estadisticas: escala 'src' para que tenga
    la misma media y desviacion estandar que 'ref'.
    Solo utiliza pixeles finitos para el calculo.
    """
    vs = src[np.isfinite(src)].ravel()
    vr = ref[np.isfinite(ref)].ravel()

    src_mean = vs.mean() if vs.size else 0.0
    src_std = vs.std() if vs.size else 1.0
    ref_mean = vr.mean() if vr.size else 0.0
    ref_std = vr.std() if vr.size else 1.0

    if src_std < 1e-10:
        return src + (ref_mean - src_mean)
    return (src - src_mean) * (ref_std / src_std) + ref_mean


def _simple_brovey(pan_arr, ms_arr, weights=None, nodata=None):
    """
    Simple Brovey Transform.
    out_i = ms_i * PAN / sum(MS_bandas)
    """
    ms_sum = np.sum(ms_arr, axis=0)
    ms_sum = np.where(np.abs(ms_sum) < 1e-10, 1e-10, ms_sum)
    result = np.empty_like(ms_arr)
    for i in range(ms_arr.shape[0]):
        result[i] = ms_arr[i] * pan_arr / ms_sum
    return result


def _gram_schmidt(pan_arr, ms_arr, weights=None, nodata=None):
    """
    Gram-Schmidt Spectral Fusion — metodo GS1 de inyeccion espectral.

    Pasos:
      1. PAN sintetico = media ponderada de las bandas MS (pesos opcionales).
      2. PAN real ajustado al histograma del PAN sintetico.
      3. sharp_i = ms_i + gain_i * (pan_adj - synth_pan)
         donde gain_i = cov(ms_i, synth_pan) / var(synth_pan).

    Si se proporcionan pesos, se usan para el PAN sintetico (mas relevante
    cuando las bandas MS tienen rangos espectrales diferentes).
    """
    n_bands = ms_arr.shape[0]

    # Pesos normalizados para el PAN sintetico
    if weights and len(weights) == n_bands:
        w = np.array(weights, dtype=np.float64)
        s = w.sum()
        w = w / \
            s if s > 1e-10 else np.ones(n_bands, dtype=np.float64) / n_bands
    else:
        w = np.ones(n_bands, dtype=np.float64) / n_bands

    # PAN sintetico (baja resolucion simulado)
    synth_pan = np.zeros(ms_arr.shape[1:], dtype=np.float64)
    for i in range(n_bands):
        synth_pan += w[i] * ms_arr[i]

    # Ajustar histograma del PAN real al PAN sintetico
    pan_adj = _histogram_match_linear(pan_arr, synth_pan)

    # Inyeccion por banda con ganancia optima por minimos cuadrados
    var_synth = np.var(synth_pan)
    delta = pan_adj - synth_pan
    result = np.empty_like(ms_arr)
    synth_flat = synth_pan.ravel()
    synth_mean = synth_flat.mean()

    for i in range(n_bands):
        if var_synth > 1e-10:
            ms_flat = ms_arr[i].ravel()
            cov = np.mean((ms_flat - ms_flat.mean()) *
                          (synth_flat - synth_mean))
            gain = cov / var_synth
        else:
            gain = 1.0
        result[i] = ms_arr[i] + gain * delta

    return result


def _hsv_fusion(pan_arr, ms_arr, weights=None, nodata=None):
    """
    HSV Pansharpening (Hue-Saturation-Value).

    Convierte las primeras 3 bandas (R, G, B) a HSV, reemplaza el canal V
    (intensidad) con el PAN ajustado estadisticamente y reconvierte.
    Bandas adicionales (> 3) se fusionan con Simple Brovey.
    """
    n_bands = ms_arr.shape[0]

    # Fallback a Brovey si hay menos de 3 bandas
    if n_bands < 3:
        return _simple_brovey(pan_arr, ms_arr, weights, nodata)

    ms3 = ms_arr[:3].astype(np.float64)

    # Normalizar RGB al rango [0, 1] usando el rango global de las 3 bandas
    global_min = ms3.min()
    global_max = ms3.max()
    drange = max(global_max - global_min, 1e-10)

    r = (ms3[0] - global_min) / drange
    g = (ms3[1] - global_min) / drange
    b = (ms3[2] - global_min) / drange

    # RGB -> HSV (vectorizado)
    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin

    v = cmax
    s = np.where(cmax > 1e-10, delta / cmax, 0.0)

    h = np.zeros_like(r)
    mask_d = delta > 1e-10
    mask_r = mask_d & (cmax == r)
    mask_g = mask_d & (cmax == g)
    mask_b = mask_d & (cmax == b)
    h[mask_r] = ((g[mask_r] - b[mask_r]) / delta[mask_r]) % 6.0
    h[mask_g] = (b[mask_g] - r[mask_g]) / delta[mask_g] + 2.0
    h[mask_b] = (r[mask_b] - g[mask_b]) / delta[mask_b] + 4.0
    h /= 6.0  # -> [0, 1]

    # Reemplazar V con PAN ajustado estadisticamente y recortado a [0, 1]
    pan_v = _histogram_match_linear(pan_arr, v)
    pan_v = np.clip(pan_v, 0.0, 1.0)

    # HSV -> RGB con nueva V
    v_new = pan_v
    h6 = h * 6.0
    hi = np.floor(h6).astype(int) % 6
    f = h6 - np.floor(h6)
    p = v_new * (1.0 - s)
    q = v_new * (1.0 - f * s)
    t_c = v_new * (1.0 - (1.0 - f) * s)

    r_new = np.zeros_like(r)
    g_new = np.zeros_like(g)
    b_new = np.zeros_like(b)

    for case_idx, (rv, gv, bv) in enumerate([
        (v_new, t_c, p),
        (q, v_new, p),
        (p, v_new, t_c),
        (p, q, v_new),
        (t_c, p, v_new),
        (v_new, p, q),
    ]):
        m = hi == case_idx
        r_new[m] = rv[m]
        g_new[m] = gv[m]
        b_new[m] = bv[m]

    # Desnormalizar al rango original
    result = np.copy(ms_arr).astype(np.float64)
    result[0] = r_new * drange + global_min
    result[1] = g_new * drange + global_min
    result[2] = b_new * drange + global_min

    # Bandas adicionales (>3): Simple Brovey
    if n_bands > 3:
        ms_sum = np.sum(ms_arr, axis=0)
        ms_sum = np.where(np.abs(ms_sum) < 1e-10, 1e-10, ms_sum)
        for i in range(3, n_bands):
            result[i] = ms_arr[i] * pan_arr / ms_sum

    return result


def _hcs_fusion(pan_arr, ms_arr, weights=None, nodata=None):
    """
    Hyperspherical Color Sharpening (HCS).

    Convierte cada pixel MS a coordenadas hiperesfericas (radio = intensidad
    euclidiana + n-1 angulos de direccion espectral), sustituye el radio con
    el PAN ajustado estadisticamente y reconvierte al espacio lineal.

    Referencia: Padwick et al. (2010) — WorldView-2 Pansharpening.
    Ventaja frente a Brovey: preserva las relaciones angulares entre bandas
    (firmas espectrales relativas) mejor que los metodos de ratio simple.
    """
    n_bands, rows, cols = ms_arr.shape
    N = rows * cols

    # Reorganizar a (N, n_bands)
    pixels = ms_arr.reshape(n_bands, N).T.astype(np.float64)  # (N, bands)
    pan_flat = pan_arr.ravel().astype(np.float64)                # (N,)

    # Intensidad euclidiana (radio en el hiperespacio espectral)
    intensity = np.sqrt(np.sum(pixels ** 2, axis=1))  # (N,)

    # Vectores unitarios de direccion
    int_safe = np.where(intensity > 1e-10, intensity, 1e-10)
    unit_vec = pixels / int_safe[:, np.newaxis]        # (N, n_bands)

    # Ajustar el PAN a las estadisticas de la intensidad MS
    pan_adj = _histogram_match_linear(pan_flat, intensity)
    pan_adj = np.maximum(pan_adj, 0.0)

    # Reconstruir con nueva intensidad = PAN
    result_pixels = unit_vec * pan_adj[:, np.newaxis]          # (N, n_bands)
    result = result_pixels.T.reshape(n_bands, rows, cols)
    return result


# Despacho de algoritmos Python/NumPy
_ALGO_FN = {
    "SimpleBrovey": _simple_brovey,
    "GS": _gram_schmidt,
    "HSV": _hsv_fusion,
    "HCS": _hcs_fusion,
}


# ---------------------------------------------------------------------------
# VRT pansharpened — WeightedBrovey via GDAL nativo
# ---------------------------------------------------------------------------

def _build_pansharpened_vrt(pan_path, pan_band, ms_paths, ms_bands,
                            algorithm, weights, resample,
                            nodata=None, bit_depth=None):
    """Genera el XML de un VRT subClass=VRTPansharpenedDataset."""
    pan_ds = gdal.Open(pan_path, gdal.GA_ReadOnly)
    if pan_ds is None:
        raise RuntimeError(f"GDAL no pudo abrir el PAN: {pan_path}")
    xs, ys = pan_ds.RasterXSize, pan_ds.RasterYSize
    pan_ds = None

    weights_xml = ""
    if weights:
        weights_xml = (
            "    <AlgorithmOptions>\n"
            f"      <Weights>{','.join(f'{float(w):.6f}' for w in weights)}"
            "</Weights>\n"
            "    </AlgorithmOptions>\n"
        )

    spectral_xml = ""
    for i, (p, b) in enumerate(zip(ms_paths, ms_bands), start=1):
        spectral_xml += (
            f'    <SpectralBand dstBand="{i}">\n'
            f'      <SourceFilename relativeToVRT="0">'
            f'{_xml_escape(p)}</SourceFilename>\n'
            f'      <SourceBand>{int(b)}</SourceBand>\n'
            f'    </SpectralBand>\n'
        )

    nodata_xml = (f"    <NoData>{nodata}</NoData>\n"
                  if nodata is not None else "")
    bitdepth_xml = (f"    <BitDepth>{int(bit_depth)}</BitDepth>\n"
                    if bit_depth is not None else "")

    vrt_xml = (
        f'<VRTDataset rasterXSize="{xs}" rasterYSize="{ys}" '
        f'subClass="VRTPansharpenedDataset">\n'
        f'  <PansharpeningOptions>\n'
        f'    <Algorithm>{algorithm}</Algorithm>\n'
        f'{weights_xml}'
        f'    <Resampling>{resample}</Resampling>\n'
        f'    <NumThreads>ALL_CPUS</NumThreads>\n'
        f'{nodata_xml}'
        f'{bitdepth_xml}'
        f'    <PanchroBand>\n'
        f'      <SourceFilename relativeToVRT="0">'
        f'{_xml_escape(pan_path)}</SourceFilename>\n'
        f'      <SourceBand>{int(pan_band)}</SourceBand>\n'
        f'    </PanchroBand>\n'
        f'{spectral_xml}'
        f'  </PansharpeningOptions>\n'
        f'</VRTDataset>\n'
    )
    return vrt_xml


# ---------------------------------------------------------------------------
# Escritura de GeoTIFF de salida (algoritmos Python)
# ---------------------------------------------------------------------------

def _write_output_geotiff(out_path, bands_arr, ms_names, pan_ds,
                          compress, algorithm_name):
    """
    Escribe GeoTIFF Float32 multibanda copiando la georreferencia del PAN.
    Asigna SetDescription y metadato BAND_NAME a cada banda.
    """
    n_bands = len(bands_arr)
    creation = ['TILED=YES', 'BIGTIFF=IF_SAFER']
    if compress and compress != 'NONE':
        creation.append(f'COMPRESS={compress}')

    driver = gdal.GetDriverByName('GTiff')
    ds_out = driver.Create(
        out_path,
        pan_ds.RasterXSize, pan_ds.RasterYSize,
        n_bands, gdal.GDT_Float32,
        options=creation,
    )
    if ds_out is None:
        raise RuntimeError(
            f"No se pudo crear el GeoTIFF de salida: {out_path}")

    ds_out.SetGeoTransform(pan_ds.GetGeoTransform())
    ds_out.SetProjection(pan_ds.GetProjection())

    for i, (arr, nm) in enumerate(zip(bands_arr, ms_names), start=1):
        band = ds_out.GetRasterBand(i)
        band.WriteArray(arr.astype(np.float32))
        band.SetDescription(nm)
        try:
            band.SetMetadataItem('BAND_NAME', nm)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"No se pudo etiquetar la banda {i} ('{nm}'): {e}",
                "Geomaticape", Qgis.Warning)

    try:
        ds_out.SetMetadataItem('GEOMATICAPE_BAND_ORDER', ','.join(ms_names))
        ds_out.SetMetadataItem('GEOMATICAPE_PANSHARP_ALG', algorithm_name)
    except Exception as e:
        QgsMessageLog.logMessage(
            f"No se pudieron escribir los metadatos de pansharpening: {e}",
            "Geomaticape", Qgis.Warning)

    ds_out.FlushCache()
    ds_out = None


# ---------------------------------------------------------------------------
# Logica principal
# ---------------------------------------------------------------------------

def ejecutar_pansharpening(ms_paths, ms_bands, ms_names,
                           pan_path, pan_band,
                           out_path, algorithm, weights, resample,
                           compress, feedback,
                           nodata=None, bit_depth=None):
    """Genera el GeoTIFF pansharpeneado a la resolucion del PAN."""

    if not ms_paths or len(ms_paths) < 1:
        raise RuntimeError("Agrega al menos una banda multiespectral.")
    if not pan_path:
        raise RuntimeError("Selecciona la banda pancromatica.")
    if not (len(ms_paths) == len(ms_bands) == len(ms_names)):
        raise RuntimeError(
            "Listas inconsistentes (paths/bands/names) en la entrada MS."
        )

    n_ms = len(ms_paths)

    # Pesos por defecto uniformes
    if not weights:
        weights = [1.0 / n_ms] * n_ms
    elif len(weights) != n_ms:
        raise RuntimeError(
            f"La lista de pesos tiene {len(weights)} valores "
            f"pero hay {n_ms} bandas MS."
        )

    pan_info = _open_info(pan_path)
    feedback.pushInfo("=" * 64)
    feedback.pushInfo("Landsat Pansharpening 30 m -> 15 m")
    feedback.pushInfo(f"Algoritmo : {algorithm}")
    feedback.pushInfo(f"Remuestreo: {resample}")
    feedback.pushInfo(
        f"PAN       : {os.path.basename(pan_path)} "
        f"(banda {pan_band}, {pan_info['cols']}x{pan_info['rows']} px, "
        f"px ~{abs(pan_info['gt'][1]):.2f} m)"
    )
    feedback.pushInfo("Bandas multiespectrales (orden = orden de salida):")
    for i, (p, b, n, w) in enumerate(
            zip(ms_paths, ms_bands, ms_names, weights), 1):
        feedback.pushInfo(
            f"  Banda {i:2d}: {n:<14s} (peso {w:.3f})  <-  "
            f"{os.path.basename(p)} (banda origen: {b})"
        )
    feedback.pushInfo("=" * 64)
    feedback.setProgress(5)

    out_dir = os.path.dirname(out_path) or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)

    if algorithm == "WeightedBrovey":
        _ejecutar_weighted_brovey(
            ms_paths, ms_bands, ms_names,
            pan_path, pan_band, out_path,
            weights, resample, compress, feedback, nodata, bit_depth,
        )
    else:
        _ejecutar_numpy_algo(
            ms_paths, ms_bands, ms_names,
            pan_path, pan_band, out_path,
            algorithm, weights, resample, compress, feedback, nodata,
        )

    feedback.pushInfo("=" * 64)
    feedback.pushInfo(f"OK - Raster pansharpeneado: {out_path}")
    feedback.pushInfo(f"Bandas: {', '.join(ms_names)}")
    feedback.pushInfo("=" * 64)
    feedback.setProgress(100)
    return out_path


def _ejecutar_weighted_brovey(ms_paths, ms_bands, ms_names,
                              pan_path, pan_band, out_path,
                              weights, resample, compress,
                              feedback, nodata, bit_depth):
    """WeightedBrovey via GDAL VRT pansharpened nativo."""
    vrt_path = os.path.splitext(out_path)[0] + "_pansharp.vrt"

    feedback.pushInfo("Construyendo VRT pansharpened (WeightedBrovey)...")
    vrt_xml = _build_pansharpened_vrt(
        pan_path=pan_path, pan_band=pan_band,
        ms_paths=ms_paths, ms_bands=ms_bands,
        algorithm="WeightedBrovey", weights=weights,
        resample=resample, nodata=nodata, bit_depth=bit_depth,
    )
    with open(vrt_path, "w", encoding="utf-8") as fh:
        fh.write(vrt_xml)

    test = gdal.Open(vrt_path, gdal.GA_ReadOnly)
    if test is None:
        raise RuntimeError(
            "GDAL no pudo interpretar el VRT pansharpened. "
            "Revisa que las bandas multiespectrales y la PAN sean validas "
            "y compartan al menos un sistema de referencia."
        )
    test = None
    feedback.setProgress(20)

    feedback.pushInfo("Escribiendo GeoTIFF de salida (puede tardar)...")
    creation = ["TILED=YES", "BIGTIFF=IF_SAFER"]
    if compress and compress != "NONE":
        creation.append(f"COMPRESS={compress}")

    def _gdal_progress(pct, message, user_data):
        if feedback.isCanceled():
            return 0
        try:
            feedback.setProgress(20 + int(pct * 70))
        except Exception as e:
            QgsMessageLog.logMessage(
                f"No se pudo actualizar el progreso de GDAL: {e}",
                "Geomaticape", Qgis.Warning)
        return 1

    try:
        gdal.Translate(
            out_path, vrt_path,
            creationOptions=creation,
            callback=_gdal_progress,
        )
    except Exception as e:
        try:
            os.remove(vrt_path)
        except OSError:
            pass
        raise RuntimeError(f"gdal.Translate fallo: {e}")

    feedback.setProgress(92)

    # Escribir nombres de banda en el GeoTIFF de salida
    ds_out = gdal.Open(out_path, gdal.GA_Update)
    if ds_out is None:
        raise RuntimeError(
            f"No se pudo abrir el GeoTIFF para escribir nombres: {out_path}"
        )
    for i, nm in enumerate(ms_names, start=1):
        b = ds_out.GetRasterBand(i)
        b.SetDescription(nm)
        try:
            b.SetMetadataItem("BAND_NAME", nm)
        except Exception as e:
            feedback.pushInfo(
                f"Aviso: no se pudo etiquetar la banda {i} ('{nm}'): {e}")
    try:
        ds_out.SetMetadataItem("GEOMATICAPE_BAND_ORDER", ",".join(ms_names))
        ds_out.SetMetadataItem("GEOMATICAPE_PANSHARP_ALG", "WeightedBrovey")
        ds_out.SetMetadataItem("GEOMATICAPE_PANSHARP_RESAMPLE", resample)
        ds_out.SetMetadataItem(
            "GEOMATICAPE_PANSHARP_WEIGHTS",
            ",".join(f"{w:.6f}" for w in weights),
        )
    except Exception as e:
        feedback.pushInfo(
            f"Aviso: no se pudieron escribir los metadatos de "
            f"pansharpening: {e}")
    ds_out.FlushCache()
    ds_out = None

    try:
        os.remove(vrt_path)
    except OSError:
        pass


def _ejecutar_numpy_algo(ms_paths, ms_bands, ms_names,
                         pan_path, pan_band, out_path,
                         algorithm, weights, resample, compress,
                         feedback, nodata):
    """Algoritmos Python/NumPy: SimpleBrovey, GS, HSV, HCS."""
    algo_fn = _ALGO_FN.get(algorithm)
    if algo_fn is None:
        raise RuntimeError(f"Algoritmo desconocido: '{algorithm}'")

    # Cargar PAN
    feedback.pushInfo("Cargando banda pancromatica...")
    pan_ds = _load_pan_ds(pan_path)
    pan_b = pan_ds.GetRasterBand(int(pan_band))
    pan_arr = pan_b.ReadAsArray().astype(np.float64)
    pan_nd = pan_b.GetNoDataValue()
    if pan_nd is not None:
        pan_arr[pan_arr == pan_nd] = np.nan
    feedback.setProgress(10)

    # Remuestrear bandas MS al grid del PAN
    feedback.pushInfo("Remuestreando bandas MS al grid del PAN...")
    ms_arrays = []
    n_ms = len(ms_paths)
    nd_used = nodata

    for k, (p, b) in enumerate(zip(ms_paths, ms_bands)):
        if feedback.isCanceled():
            pan_ds = None
            raise RuntimeError("Operacion cancelada por el usuario.")
        feedback.pushInfo(
            f"  [{k + 1}/{n_ms}] {os.path.basename(p)} (banda {b}) ..."
        )
        arr, nd = _resample_band_to_pan(p, int(b), pan_ds, resample)
        if nd is not None and nd_used is None:
            nd_used = nd
        if nd is not None:
            arr[arr == nd] = np.nan
        ms_arrays.append(arr)
        feedback.setProgress(10 + int((k + 1) * 40 / n_ms))

    # Stack MS: (n_bands, rows, cols)
    ms_arr = np.stack(ms_arrays, axis=0)

    # Mascara de pixeles sin datos
    nan_pan = np.isnan(pan_arr)
    nan_ms = np.any(np.isnan(ms_arr), axis=0)

    # Reemplazar NaN con 0 para los calculos
    pan_proc = np.where(nan_pan, 0.0, pan_arr)
    ms_proc = np.where(np.isnan(ms_arr), 0.0, ms_arr)

    feedback.pushInfo(f"Aplicando algoritmo {algorithm}...")
    feedback.setProgress(55)

    result = algo_fn(pan_proc, ms_proc, weights=weights, nodata=nd_used)

    # Aplicar mascara de sin datos en la salida
    nan_total = nan_pan | nan_ms
    for i in range(result.shape[0]):
        result[i][nan_total] = 0.0

    feedback.setProgress(80)
    feedback.pushInfo("Escribiendo GeoTIFF de salida (Float32)...")

    _write_output_geotiff(
        out_path,
        [result[i] for i in range(result.shape[0])],
        ms_names,
        pan_ds,
        compress,
        algorithm,
    )

    pan_ds = None
    feedback.setProgress(98)


def _open_info(path):
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL no pudo abrir: {path}")
    info = {
        "cols": ds.RasterXSize,
        "rows": ds.RasterYSize,
        "gt": ds.GetGeoTransform(),
        "proj": ds.GetProjection(),
        "nbands": ds.RasterCount,
    }
    ds = None
    return info


# ---------------------------------------------------------------------------
# Dialogo de seleccion de UNA capa raster QGIS (para carga multibanda)
# ---------------------------------------------------------------------------

class LandsatPansharpeningDialog(QDialog):
    """
    Interfaz nativa de QGIS para pansharpening:
      1. Banda Multiespectral (30 m) - QgsMapLayerComboBox
      2. Banda Pancromatica (15 m) - QgsMapLayerComboBox
      3. Algoritmo Pansharpening
      4. Remuestreo Pixel
      5. Salida Raster 15 m - QgsFileWidget
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Landsat Pansharpening 30m -> 15m")
        self.resize(580, 360)
        self._build_ui()

    def _build_ui(self):
        main = QVBoxLayout(self)
        main.setSpacing(10)
        main.setContentsMargins(15, 15, 15, 15)

        desc = QLabel(
            "<b>Landsat Pansharpening</b> &mdash; Fusiona todas las bandas "
            "del raster multiespectral (30&nbsp;m) con la banda pancrom&aacute;tica "
            "(15&nbsp;m). Los nombres de banda se preservan en la salida."
        )
        desc.setWordWrap(True)
        main.addWidget(desc)

        sep = QLabel()
        sep.setFixedHeight(4)
        main.addWidget(sep)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(14)

        # 1. MS
        self.cb_ms = QgsMapLayerComboBox()
        self.cb_ms.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)
        self.cb_ms.layerChanged.connect(self._update_ms_info)
        form.addRow("<b>Banda Multiespectral 30m</b>", self.cb_ms)

        self.lbl_ms_info = QLabel("")
        self.lbl_ms_info.setStyleSheet(
            "color:#4a7c59; font-size:11px; padding-left:2px;")
        form.addRow("", self.lbl_ms_info)

        # 2. PAN
        self.cb_pan = QgsMapLayerComboBox()
        self.cb_pan.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)
        form.addRow("<b>Banda Pancrom&aacute;tica 15m</b>", self.cb_pan)

        # 3. Algoritmo
        self.combo_alg = QComboBox()
        self.combo_alg.addItems(PANSHARP_ALGORITHMS)
        self.combo_alg.setCurrentText("Gram-Schmidt (GS)")
        self.combo_alg.currentTextChanged.connect(self._on_alg_changed)
        form.addRow("<b>Algoritmo Pansharpening</b>", self.combo_alg)

        self.lbl_alg_desc = QLabel("")
        self.lbl_alg_desc.setWordWrap(True)
        self.lbl_alg_desc.setStyleSheet(
            "color:#555; font-size:11px; padding-left:2px;")
        form.addRow("", self.lbl_alg_desc)

        # 4. Remuestreo
        self.combo_resample = QComboBox()
        self.combo_resample.addItems(PAN_RESAMPLE)
        self.combo_resample.setCurrentText("Cubic")
        form.addRow("<b>Remuestreo Pixel</b>", self.combo_resample)

        # 5. Salida
        self.fw_out = QgsFileWidget()
        self.fw_out.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.fw_out.setFilter("GeoTIFF (*.tif *.tiff)")
        self.fw_out.setDialogTitle("Guardar raster de salida")

        try:
            le = self.fw_out.findChild(QLineEdit)
            if le:
                le.setPlaceholderText("[Guardar en archivo temporal]")
        except BaseException:
            pass
        form.addRow("<b>Salida Raster 15m</b>", self.fw_out)

        main.addLayout(form)
        main.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Ejecutar")
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        main.addWidget(bb)

        self._update_ms_info(self.cb_ms.currentLayer())
        self._on_alg_changed(self.combo_alg.currentText())

    def _update_ms_info(self, layer):
        if not layer:
            self.lbl_ms_info.setText(
                "⚠ Selecciona una capa raster multiespectral.")
            self.lbl_ms_info.setStyleSheet(
                "color:#b05000; font-size:11px; padding-left:2px;")
            self._ms_names = []
            self._ms_nbands = 0
            return

        path = layer.source()
        nb = _band_count(path)
        if nb > 0:
            names = []
            for i in range(1, nb + 1):
                nm = _detect_band_name_at(path, i) or _default_name(path, i)
                names.append(_safe_name(nm))
            preview = ", ".join(names[:5])
            if nb > 5:
                preview += f", ... (+{nb - 5} más)"
            self.lbl_ms_info.setText(
                f"✓ {nb} banda(s) detectada(s): {preview}")
            self.lbl_ms_info.setStyleSheet(
                "color:#3a6b4a; font-size:11px; padding-left:2px;")
            self._ms_names = names
            self._ms_nbands = nb
        else:
            self.lbl_ms_info.setText(
                "⚠ No se detectaron bandas v&aacute;lidas.")
            self.lbl_ms_info.setStyleSheet(
                "color:#b05000; font-size:11px; padding-left:2px;")
            self._ms_names = []
            self._ms_nbands = 0

    def _on_alg_changed(self, algo_display):
        algo = _ALGO_DISPLAY_MAP.get(algo_display, "")
        desc = _ALGO_DESCRIPTIONS.get(algo, "")
        self.lbl_alg_desc.setText(desc)

    def _on_ok(self):
        ms_lyr = self.cb_ms.currentLayer()
        if not ms_lyr:
            QMessageBox.warning(
                self,
                "Pansharpening",
                "Selecciona el raster multiespectral (30 m).")
            return

        if getattr(self, '_ms_nbands', 0) == 0:
            QMessageBox.warning(
                self,
                "Pansharpening",
                "El raster multiespectral no tiene bandas v&aacute;lidas.")
            return

        pan_lyr = self.cb_pan.currentLayer()
        if not pan_lyr:
            QMessageBox.warning(
                self,
                "Pansharpening",
                "Selecciona la banda pancrom&aacute;tica (15 m).")
            return

        out_path = self.fw_out.filePath().strip()
        if not out_path:
            import tempfile
            out_path = os.path.join(
                tempfile.gettempdir(),
                f"_geomaticape_pansharpening_{
                    os.getpid()}.tif")
        else:
            if not out_path.lower().endswith((".tif", ".tiff")):
                out_path += ".tif"

        n = self._ms_nbands
        ms_paths = [ms_lyr.source()] * n
        ms_bands = list(range(1, n + 1))
        ms_names = list(self._ms_names)

        pan_band = 1  # Fijo a la primera banda como se solicito

        algo_display = self.combo_alg.currentText()
        algorithm = _ALGO_DISPLAY_MAP.get(algo_display, "GS")
        resample = self.combo_resample.currentText()
        compress = "LZW"

        progress = QProgressDialog(
            "Aplicando pansharpening...", "Cancelar", 0, 100, self)
        progress.setWindowTitle(f"Pansharpening — {algorithm}")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setAutoClose(True)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        QApplication.processEvents()

        feedback = _DialogFeedback(progress)
        try:
            ejecutar_pansharpening(
                ms_paths=ms_paths, ms_bands=ms_bands, ms_names=ms_names,
                pan_path=pan_lyr.source(), pan_band=pan_band,
                out_path=out_path,
                algorithm=algorithm, weights=None,
                resample=resample, compress=compress,
                feedback=feedback,
            )
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Pansharpening — Error", str(e))
            return

        progress.close()

        # Cargar resultado en QGIS
        try:
            lyr = QgsRasterLayer(out_path, os.path.basename(out_path))
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"No se pudo cargar '{out_path}' en el proyecto: {e}",
                "Geomaticape", Qgis.Warning)

        QMessageBox.information(
            self, "Pansharpening completado",
            f"Algoritmo : {algo_display}\n"
            f"Salida    : {out_path}\n\n"
            f"Bandas a 15 m:\n  " + "\n  ".join(
                f"{i + 1}. {nm}" for i, nm in enumerate(ms_names)
            )
        )
        self.accept()

# ---------------------------------------------------------------------------
# Wrapper invocado desde el menu Geomaticape -> Procesamiento
# ---------------------------------------------------------------------------


class LandsatPansharpening(GeomaticapeAlgorithm):
    """Lanzador desde el menu del plugin."""

    _algorithm_name = "landsat_pansharpening"
    _icon_name = "landsat.png"

    def __init__(self, iface=None):
        super().__init__()
        self.iface = iface

    def displayName(self):
        return self.tr("Landsat Pansharpening (30m -> 15m)")

    def group(self):
        return self.tr("Procesamiento")

    def groupId(self):
        return "geomaticape_procesamiento"

    def shortHelpString(self):
        return self.tr(
            "Herramienta interactiva de Pansharpening. Úsela desde el menú.")

    def initAlgorithm(self, config=None):
        pass

    def processAlgorithm(self, parameters, context, feedback):
        raise QgsProcessingException(
            "Esta herramienta requiere interacción manual. Ejecútela desde el menú Geomaticape.")

    def icon(self):
        import os
        from qgis.PyQt.QtGui import QIcon
        return QIcon(os.path.join(
            os.path.dirname(__file__), "..", "Icons", self._icon_name
        ))

    def run(self):
        parent = None
        try:
            from qgis.utils import iface as _qgis_iface
            if _qgis_iface is not None:
                parent = _qgis_iface.mainWindow()
        except Exception:
            parent = None
        dlg = LandsatPansharpeningDialog(parent=parent)
        qt_exec(dlg)
