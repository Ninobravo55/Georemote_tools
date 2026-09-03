from ._qt_compat import qt_exec
from .geomaticape_algorithm import GeomaticapeAlgorithm
"""
Clasificacion supervisada y validacion
=======================================
Para cualquier imagen multiespectral entrena un clasificador supervisado a partir
de un shapefile de poligonos ROI y genera:

  - roi_train.shp              (puntos de entrenamiento - fold 1)
  - roi_test.shp               (puntos de validacion - fold 1)
  - clasificacion.tif          (raster clasificado)
  - clasificacion_vector.shp   (vector clasificado)
  - matriz_confusion.csv       (matriz de confusion acumulada)
  - metricas_globales.csv      (OA, Kappa, F1 macro/weighted/micro)
  - metricas_por_clase.csv     (precision, recall, especificidad, F1 por clase)
  - importancia_bandas.csv     (importancia relativa de cada banda/variable)
  - reporte_clasificacion.html (Reporte tecnico completo con identidad institucional)

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape v1.10
Grupo : Geoprocesamiento
"""

import os
import csv
import gc
import base64
import datetime
import numpy as np

from qgis.PyQt.QtCore import QVariant, QTimer
from qgis.core import (
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFolderDestination,
    QgsProcessingException,
    QgsProcessing,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsVectorLayer,
    QgsField, QgsFields,
    QgsFeature, QgsGeometry, QgsPointXY, QgsVectorFileWriter
)
from qgis import processing
from osgeo import gdal, ogr, osr
from qgis.utils import iface

# Helper to show UI message safely


def _show_missing_library(lib_name, pip_cmd):
    def show():
        from qgis.PyQt.QtWidgets import QMessageBox
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Critical)
        msg.setWindowTitle("Librería faltante")
        msg.setText(
            f"Se requiere instalar '{lib_name}' para usar este algoritmo.")
        msg.setInformativeText(
            f"Por favor, ve al menú GeomaticaPe -> 'Instalar dependencias de Python...' para instalar las librerías necesarias.\n\nComando manual:\n{pip_cmd}")
        if iface:
            msg.setParent(iface.mainWindow(), msg.windowFlags())
        qt_exec(msg)
    QTimer.singleShot(0, show)
    raise QgsProcessingException(
        f"Librería requerida no encontrada: {lib_name}. Por favor use 'Instalar dependencias de Python' en el menú.")


def _info_raster(path):
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise QgsProcessingException(f"GDAL no pudo abrir {path}")
    info = {
        "cols": ds.RasterXSize,
        "rows": ds.RasterYSize,
        "nbands": ds.RasterCount,
        "gt": ds.GetGeoTransform(),
        "proj": ds.GetProjection(),
        "px": abs(ds.GetGeoTransform()[1]),
        "py": abs(ds.GetGeoTransform()[5]),
        "band_names": [],
        "nodata": [],
    }
    for i in range(1, ds.RasterCount + 1):
        b = ds.GetRasterBand(i)
        nm = b.GetDescription()
        info["band_names"].append(nm if nm else f"Banda_{i}")
        info["nodata"].append(b.GetNoDataValue())
    ds = None
    return info


def _safe_field(name, max_len=10):
    nm = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    return nm[:max_len] if len(nm) > max_len else nm


def _rasterizar_roi(roi_path, info, campo_id, dst_path):
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(
        dst_path,
        info["cols"],
        info["rows"],
        1,
        gdal.GDT_Int32,
        options=[
            "COMPRESS=LZW",
            "TILED=YES",
            "BIGTIFF=IF_SAFER"])
    out.SetGeoTransform(info["gt"])
    out.SetProjection(info["proj"])
    band = out.GetRasterBand(1)
    band.SetNoDataValue(-9999)
    band.Fill(-9999)

    src = ogr.Open(roi_path)
    layer = src.GetLayer()
    gdal.RasterizeLayer(out, [1], layer, options=[f"ATTRIBUTE={campo_id}"])
    out.FlushCache()
    out = None
    src = None


def _escribir_shp_puntos(ruta, gt, proj, datos_filas, columnas, tipos):
    fields = QgsFields()
    for nm, t in zip(columnas, tipos):
        if t == QVariant.Double:
            fields.append(QgsField(nm, QVariant.Double, "double", 20, 6))
        elif t == QVariant.Int:
            fields.append(QgsField(nm, QVariant.Int))
        elif t == QVariant.LongLong:
            fields.append(QgsField(nm, QVariant.LongLong))
        else:
            fields.append(QgsField(nm, QVariant.String, len=80))

    crs_authid = ""
    sref = osr.SpatialReference()
    if proj:
        sref.ImportFromWkt(proj)
        if sref.GetAuthorityCode(None):
            crs_authid = f"{
                sref.GetAuthorityName(None)}:{
                sref.GetAuthorityCode(None)}"

    crs_str = crs_authid if crs_authid else "EPSG:4326"
    mem = QgsVectorLayer(f"Point?crs={crs_str}", "tmp_pts", "memory")
    pr = mem.dataProvider()
    pr.addAttributes(fields)
    mem.updateFields()
    feats = []
    for fila in datos_filas:
        x, y = fila[0], fila[1]
        attrs = list(fila[2:])
        f = QgsFeature(fields)
        f.setAttributes(attrs)
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        feats.append(f)
    pr.addFeatures(feats)
    mem.updateExtents()

    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "ESRI Shapefile"
    opts.fileEncoding = "UTF-8"
    err = QgsVectorFileWriter.writeAsVectorFormatV3(
        mem, ruta, QgsProject.instance().transformContext(), opts
    )
    if isinstance(err, tuple) and err[0] != QgsVectorFileWriter.WriterError.NoError:
        raise QgsProcessingException(f"No se pudo escribir {ruta}: {err[1]}")


def _matriz_confusion(y_true, y_pred, clases):
    n = len(clases)
    idx = {c: i for i, c in enumerate(clases)}
    M = np.zeros((n, n), dtype=np.int64)
    for t, p in zip(y_true, y_pred):
        if t in idx and p in idx:
            M[idx[t], idx[p]] += 1
    return M


def _kappa(M):
    M = M.astype(np.float64)
    total = M.sum()
    if total <= 0:
        return float("nan")
    po = np.trace(M) / total
    pe = (M.sum(axis=0) * M.sum(axis=1)).sum() / (total * total)
    if (1 - pe) <= 0:
        return float("nan")
    return (po - pe) / (1 - pe)


def _fig_to_base64(fig):
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=100)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('utf-8')


def _plot_matriz_b64(M, clases, oa, kappa):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return ""
    fig, ax = plt.subplots(
        figsize=(max(6, len(clases) * 0.8), max(5, len(clases) * 0.8)))
    im = ax.imshow(M, cmap="Blues")
    ax.set_xticks(range(len(clases)))
    ax.set_yticks(range(len(clases)))
    ax.set_xticklabels([str(c) for c in clases], rotation=45, ha="right")
    ax.set_yticklabels([str(c) for c in clases])
    ax.set_xlabel("Predicho")
    ax.set_ylabel("Real")
    ax.set_title(f"Matriz de Confusión\nOA={oa:.4f}  Kappa={kappa:.4f}")
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            ax.text(j, i, str(int(M[i, j])),
                    ha="center", va="center",
                    color="white" if M[i, j] > M.max() / 2 else "black")
    fig.colorbar(im, ax=ax, fraction=0.045)
    fig.tight_layout()
    b64 = _fig_to_base64(fig)
    plt.close(fig)
    return b64


def _plot_heatmap_b64(report_dict, clases_lbl, id2cls, global_clases_id):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return ""

    metrics = ['precision', 'recall', 'f1-score']
    data = []
    valid_classes = []

    for cls_id, cls_lbl in zip(global_clases_id, clases_lbl):
        # Intentar varias claves en orden de prioridad:
        # 1. Nombre de clase (target_names usado en classification_report)
        # 2. ID numérico como string
        # 3. Prefijo del label compuesto "1 - Bosque" -> "1"
        cls_name = id2cls.get(int(cls_id), str(cls_id))
        candidates = [
            cls_name,
            str(cls_id),
            str(int(cls_id)),
        ]
        key = next(
            (k for k in candidates if k in report_dict and isinstance(
                report_dict[k], dict)), None)

        if key:
            row = [report_dict[key].get(m, 0) for m in metrics]
            data.append(row)
            valid_classes.append(cls_lbl)
        else:
            # Clase no encontrada en el reporte: añadir fila de ceros para no
            # perder la clase
            data.append([0.0, 0.0, 0.0])
            valid_classes.append(cls_lbl)

    if not data:
        return ""

    data = np.array(data)
    fig, ax = plt.subplots(
        figsize=(max(5, len(valid_classes) * 0.6), max(4, len(valid_classes) * 0.5)))
    im = ax.imshow(data, cmap="YlGn", vmin=0, vmax=1)

    ax.set_xticks(np.arange(len(metrics)))
    ax.set_yticks(np.arange(len(valid_classes)))
    ax.set_xticklabels(metrics)
    ax.set_yticklabels([str(c) for c in valid_classes])
    ax.set_title("Métricas por Clase")

    for i in range(len(valid_classes)):
        for j in range(len(metrics)):
            val = data[i, j]
            ax.text(j, i, f"{val:.2f}",
                    ha="center", va="center",
                    color="white" if val > 0.6 else "black")

    fig.tight_layout()
    b64 = _fig_to_base64(fig)
    plt.close(fig)
    return b64


def _plot_importance_b64(importances, band_names):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return ""

    # Ensure no negative importances and normalize
    importances = np.maximum(importances, 0)
    if np.sum(importances) > 0:
        importances = importances / np.sum(importances) * 100

    # Sort features
    indices = np.argsort(importances)
    sorted_names = [band_names[i] for i in indices]
    sorted_imp = importances[indices]

    fig, ax = plt.subplots(
        figsize=(max(6, len(band_names) * 0.5), max(4, len(band_names) * 0.4)))

    # Horizontal bar chart
    y_pos = np.arange(len(band_names))
    ax.barh(y_pos, sorted_imp, align='center', color='#004d80')
    ax.set_yticks(y_pos)
    ax.set_yticklabels(sorted_names)
    ax.set_xlabel('Importancia Relativa (%)')
    ax.set_title('Importancia de las Bandas/Variables')

    # Add values on the bars
    for i, v in enumerate(sorted_imp):
        ax.text(v + 0.5, i, f'{v:.1f}%', va='center', fontsize=9)

    # Add a bit of padding to x-axis
    ax.set_xlim(0, max(sorted_imp) + 15)

    fig.tight_layout()
    b64 = _fig_to_base64(fig)
    plt.close(fig)
    return b64


def _interpret_oa(oa):
    """Interpretación automática de la Exactitud Global."""
    pct = oa * 100
    if pct >= 90:
        nivel = "Excelente"
        msg = (f"La exactitud global de {pct:.2f}% es excelente y supera el umbral de referencia "
               f"establecido por Anderson et al. (1976) para clasificaciones de cobertura terrestre. "
               f"El modelo discrimina con alta confiabilidad entre las clases definidas.")
    elif pct >= 80:
        nivel = "Muy buena"
        msg = (f"La exactitud global de {pct:.2f}% es muy buena y cumple con los estándares de "
               f"calidad para aplicaciones de teledetección aplicada (>80%). El modelo presenta "
               f"un rendimiento robusto, aunque puede existir confusión puntual entre clases "
               f"espectralmente similares.")
    elif pct >= 70:
        nivel = "Aceptable"
        msg = (f"La exactitud global de {pct:.2f}% es aceptable para análisis exploratorios. "
               f"Se recomienda revisar las clases con mayor confusión y considerar el aumento "
               f"de muestras de entrenamiento o la revisión de la separabilidad espectral entre clases.")
    else:
        nivel = "Insuficiente"
        msg = (f"La exactitud global de {pct:.2f}% es insuficiente para aplicaciones de mapeo formal. "
               f"Se recomienda revisar la calidad de las muestras ROI, aumentar el número de polígonos "
               f"de entrenamiento, evaluar la separabilidad espectral de las clases y considerar "
               f"algoritmos alternativos.")
    return nivel, msg


def _interpret_kappa(kappa):
    """Interpretación del índice Kappa de Cohen."""
    if kappa >= 0.81:
        return "Acuerdo casi perfecto", "El índice Kappa indica un acuerdo casi perfecto entre la clasificación y la referencia, superando ampliamente el azar (Landis & Koch, 1977)."
    elif kappa >= 0.61:
        return "Acuerdo sustancial", "El índice Kappa indica un acuerdo sustancial. El modelo clasifica significativamente mejor que una asignación aleatoria."
    elif kappa >= 0.41:
        return "Acuerdo moderado", "El índice Kappa indica un acuerdo moderado. Existe margen de mejora considerable en la discriminación entre clases."
    elif kappa >= 0.21:
        return "Acuerdo regular", "El índice Kappa indica un acuerdo regular. Se recomienda revisar el diseño muestral y la homogeneidad interna de las clases."
    else:
        return "Acuerdo leve o nulo", "El índice Kappa indica un acuerdo leve o no significativamente mejor que el azar. Se requiere una revisión integral de las muestras y del esquema de clasificación."


def _tabla_html_metricas_por_clase(
        report_dict, id2cls, global_clases_id, M_final):
    """Genera tabla HTML completa de métricas por clase incluyendo especificidad."""
    n = len(global_clases_id)
    M = M_final.astype(np.float64)
    total = M.sum()
    rows_html = ""
    for i, cls_id in enumerate(global_clases_id):
        cls_name = id2cls.get(int(cls_id), str(cls_id))
        cls_key = cls_name
        if cls_key not in report_dict:
            cls_key = str(cls_id)
        if cls_key not in report_dict:
            cls_key = str(int(cls_id))
        rd = report_dict.get(cls_key, {})
        prec = rd.get('precision', 0.0)
        rec = rd.get('recall', 0.0)
        f1 = rd.get('f1-score', 0.0)
        sup = int(rd.get('support', 0))
        # Especificidad = TN / (TN + FP)
        tp = M[i, i]
        fn = M[i, :].sum() - tp
        fp = M[:, i].sum() - tp
        tn = total - tp - fn - fp
        spec = tn / (tn + fp) if (tn + fp) > 0 else 0.0

        def color_cell(v):
            if v >= 0.90:
                bg, fg = "#1a7a4a", "white"
            elif v >= 0.80:
                bg, fg = "#2E8B57", "white"
            elif v >= 0.70:
                bg, fg = "#85bb9a", "#1a3a2a"
            elif v >= 0.60:
                bg, fg = "#f0b429", "#3a2a00"
            else:
                bg, fg = "#d94f4f", "white"
            return f'style="background:{bg};color:{fg};font-weight:600;text-align:center;padding:8px 6px;border-radius:4px;"'

        rows_html += f"""
        <tr>
          <td style="padding:9px 12px;font-weight:600;color:#1e3a5f;border-bottom:1px solid #e8edf2;">{cls_id} – {cls_name}</td>
          <td {color_cell(prec)}>{prec:.4f}</td>
          <td {color_cell(rec)}>{rec:.4f}</td>
          <td {color_cell(spec)}>{spec:.4f}</td>
          <td {color_cell(f1)}>{f1:.4f}</td>
          <td style="text-align:center;padding:8px 6px;color:#475569;border-bottom:1px solid #e8edf2;">{sup:,}</td>
        </tr>"""
    return rows_html


def _tabla_html_importancia(importances, band_names):
    """Genera tabla HTML de importancia de bandas."""
    imp = np.maximum(importances, 0)
    if imp.sum() > 0:
        imp = imp / imp.sum() * 100
    orden = np.argsort(imp)[::-1]
    rows = ""
    for rank, idx in enumerate(orden, 1):
        pct = imp[idx]
        bar_w = int(pct)
        rows += f"""
        <tr>
          <td style="padding:8px 12px;text-align:center;font-weight:700;color:#004d80;">{rank}</td>
          <td style="padding:8px 12px;font-weight:600;color:#1e3a5f;">{band_names[idx]}</td>
          <td style="padding:8px 12px;">
            <div style="display:flex;align-items:center;gap:8px;">
              <div style="flex:1;background:#e8f0e8;border-radius:4px;height:12px;">
                <div style="width:{min(bar_w, 100)}%;background:linear-gradient(90deg,#2E8B57,#004d80);height:100%;border-radius:4px;"></div>
              </div>
              <span style="font-weight:700;color:#004d80;min-width:52px;">{pct:.2f}%</span>
            </div>
          </td>
        </tr>"""
    return rows


def _generate_html_report(out_dir, method_name, val_method_name, metrics_dict, M, clases_lbl,
                          report_text, img_cm_b64, img_heatmap_b64, img_imp_b64,
                          metodologia_desc, importances, band_names, id2cls, global_clases_id,
                          report_dict, raster_name, roi_name, n_pixeles, n_poligonos,
                          n_folds, seed, logo_b64):
    now = datetime.datetime.now()
    fecha_str = now.strftime("%d de %B de %Y, %H:%M")

    oa = metrics_dict.get('OA', 0)
    kappa = metrics_dict.get('Kappa', 0)
    f1m = metrics_dict.get('F1_macro', 0)
    f1w = metrics_dict.get('F1_weighted', 0)
    f1mi = metrics_dict.get('F1_micro', 0)

    nivel_oa, interp_oa = _interpret_oa(oa)
    nivel_kappa, interp_kappa = _interpret_kappa(kappa)

    # Interpretación automática de importancia de variables
    imp_norm = np.maximum(importances, 0)
    if imp_norm.sum() > 0:
        imp_norm = imp_norm / imp_norm.sum() * 100
    top_idx = int(np.argmax(imp_norm))
    top_band = band_names[top_idx]
    top_pct = imp_norm[top_idx]
    imp_interp = (
        f"La variable con mayor poder discriminatorio es <strong>{top_band}</strong> "
        f"({top_pct:.1f}% de importancia relativa), lo que sugiere que esta banda o índice "
        f"presenta la mayor variabilidad espectral entre las clases definidas. "
        f"Variables con importancia superior al 15% son críticas para el modelo; "
        f"aquellas por debajo del 5% pueden ser candidatas a exclusión para simplificar "
        f"el modelo sin pérdida significativa de exactitud."
    )

    tabla_clases_rows = _tabla_html_metricas_por_clase(
        report_dict, id2cls, global_clases_id, M)
    tabla_imp_rows = _tabla_html_importancia(importances, band_names)
    n_clases = len(global_clases_id)
    clases_nombres = ", ".join([id2cls.get(int(c), str(c))
                               for c in global_clases_id])

    logo_tag = f'<img src="data:image/jpeg;base64,{logo_b64}" alt="Geomatica Ambiental" style="height:60px;">' if logo_b64 else '<span style="font-size:22px;font-weight:800;color:#fff;">Geomatica Ambiental</span>'

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Reporte de Clasificación Supervisada – Geomatica Ambiental</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@400;600;700&family=DM+Sans:wght@300;400;500;600;700&display=swap');
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --azul:      #004d80;
      --azul-mid:  #0069aa;
      --azul-clr:  #e6f0f8;
      --verde:     #2E8B57;
      --verde-clr: #e8f3ec;
      --gris-bg:   #f0f4f8;
      --gris-ln:   #dde4ed;
      --text:      #1c2b3a;
      --text-2:    #445566;
      --white:     #ffffff;
      --serif:     'Source Serif 4', Georgia, serif;
      --sans:      'DM Sans', 'Segoe UI', sans-serif;
      --shadow:    0 2px 16px rgba(0,77,128,.09);
      --radius:    10px;
    }}
    body {{ font-family: var(--sans); background: var(--gris-bg); color: var(--text); font-size: 15px; line-height: 1.65; }}

    /* ── PORTADA ── */
    .cover {{
      background: linear-gradient(155deg, #003660 0%, #004d80 45%, #006633 100%);
      color: #fff; min-height: 340px; padding: 0;
      display: flex; flex-direction: column;
    }}
    .cover-top {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 28px 48px; border-bottom: 1px solid rgba(255,255,255,.15);
    }}
    .cover-badge {{
      background: rgba(255,255,255,.12); border: 1px solid rgba(255,255,255,.25);
      border-radius: 6px; padding: 6px 16px; font-size: 13px; font-weight: 600;
      letter-spacing: .5px; color: rgba(255,255,255,.9);
    }}
    .cover-body {{
      flex: 1; display: flex; flex-direction: column;
      justify-content: center; padding: 40px 48px 48px;
    }}
    .cover-pretitle {{
      font-size: 12px; font-weight: 700; letter-spacing: 2.5px;
      text-transform: uppercase; color: rgba(255,255,255,.6); margin-bottom: 14px;
    }}
    .cover-title {{
      font-family: var(--serif); font-size: 36px; font-weight: 700;
      line-height: 1.2; color: #fff; margin-bottom: 18px;
      border-left: 5px solid #7ed6a0; padding-left: 20px;
    }}
    .cover-meta {{
      display: flex; flex-wrap: wrap; gap: 24px; margin-top: 20px;
    }}
    .cover-meta-item {{
      display: flex; flex-direction: column; gap: 3px;
    }}
    .cover-meta-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: rgba(255,255,255,.55); }}
    .cover-meta-value {{ font-size: 14px; font-weight: 600; color: rgba(255,255,255,.95); }}

    /* ── NAVEGACIÓN / TOC ── */
    .toc {{
      background: var(--white); border-bottom: 3px solid var(--verde);
      padding: 18px 48px;
    }}
    .toc-title {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; color: var(--azul); margin-bottom: 10px; }}
    .toc-list {{ display: flex; flex-wrap: wrap; gap: 6px 20px; list-style: none; }}
    .toc-list li a {{
      font-size: 13px; color: var(--text-2); text-decoration: none;
      padding: 3px 10px; border-radius: 20px; border: 1px solid var(--gris-ln);
      transition: all .2s;
    }}
    .toc-list li a:hover {{ background: var(--azul); color: #fff; border-color: var(--azul); }}

    /* ── LAYOUT PRINCIPAL ── */
    .main {{ max-width: 1100px; margin: 0 auto; padding: 40px 24px 60px; }}

    /* ── SECCIONES ── */
    .section {{ margin-bottom: 36px; scroll-margin-top: 20px; }}
    .section-header {{
      display: flex; align-items: center; gap: 14px;
      margin-bottom: 20px; padding-bottom: 12px;
      border-bottom: 2px solid var(--gris-ln);
    }}
    .section-num {{
      width: 36px; height: 36px; border-radius: 50%;
      background: var(--azul); color: #fff;
      display: flex; align-items: center; justify-content: center;
      font-size: 14px; font-weight: 700; flex-shrink: 0;
    }}
    .section-title {{ font-family: var(--serif); font-size: 22px; font-weight: 700; color: var(--azul); }}

    /* ── CARDS ── */
    .card {{
      background: var(--white); border-radius: var(--radius);
      box-shadow: var(--shadow); padding: 26px 28px;
      margin-bottom: 20px; border-left: 4px solid var(--verde);
    }}
    .card-blue {{ border-left-color: var(--azul); }}
    .card-title {{ font-family: var(--serif); font-size: 17px; font-weight: 600; color: var(--azul); margin-bottom: 14px; }}

    /* ── MÉTRICAS KPI ── */
    .kpi-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap: 16px;
    }}
    .kpi {{
      background: var(--gris-bg); border-radius: 8px; padding: 18px 14px;
      text-align: center; border-top: 3px solid var(--azul); position: relative;
    }}
    .kpi.green {{ border-top-color: var(--verde); }}
    .kpi-label {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .8px; color: var(--text-2); }}
    .kpi-val {{ font-family: var(--serif); font-size: 30px; font-weight: 700; color: var(--azul); margin: 6px 0 2px; }}
    .kpi.green .kpi-val {{ color: var(--verde); }}
    .kpi-badge {{
      display: inline-block; font-size: 10px; font-weight: 700; padding: 2px 8px;
      border-radius: 12px; background: var(--verde-clr); color: var(--verde);
      margin-top: 4px;
    }}
    .kpi-badge.warn {{ background: #fff3cd; color: #856404; }}
    .kpi-badge.danger {{ background: #fde8e8; color: #9b1c1c; }}

    /* ── TABLAS ── */
    .tbl-wrap {{ overflow-x: auto; border-radius: 8px; box-shadow: var(--shadow); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; background: #fff; }}
    thead th {{
      background: var(--azul); color: #fff; padding: 11px 12px;
      text-align: center; font-size: 12px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .5px;
    }}
    thead th:first-child {{ text-align: left; }}
    tbody tr:nth-child(even) {{ background: #f7fafd; }}
    tbody tr:hover {{ background: var(--azul-clr); }}

    /* ── GRÁFICOS ── */
    .charts-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(320px,1fr));
      gap: 24px; align-items: start;
    }}
    .chart-box {{ background: var(--white); border-radius: var(--radius); box-shadow: var(--shadow); padding: 20px; text-align: center; }}
    .chart-box h4 {{ font-family: var(--serif); font-size: 15px; color: var(--azul); margin-bottom: 14px; font-weight: 600; }}
    .chart-box img {{ max-width: 100%; border-radius: 6px; }}
    .chart-full {{ grid-column: 1 / -1; }}

    /* ── INTERPRETACIÓN ── */
    .interp {{
      background: var(--azul-clr); border-left: 4px solid var(--azul);
      border-radius: 0 8px 8px 0; padding: 16px 20px; margin-top: 16px;
      font-size: 14px; color: var(--text); line-height: 1.7;
    }}
    .interp strong {{ color: var(--azul); }}

    /* ── INFO BLOCK ── */
    .info-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px,1fr)); gap: 14px; }}
    .info-item {{ background: var(--gris-bg); border-radius: 8px; padding: 14px 16px; }}
    .info-label {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .8px; color: var(--text-2); margin-bottom: 5px; }}
    .info-value {{ font-size: 14px; font-weight: 600; color: var(--text); word-break: break-all; }}

    /* ── REFERENCIAS ── */
    .ref-list {{ list-style: none; counter-reset: refs; }}
    .ref-list li {{ counter-increment: refs; padding: 7px 0 7px 36px; position: relative; border-bottom: 1px solid var(--gris-ln); font-size: 13px; color: var(--text-2); line-height: 1.55; }}
    .ref-list li::before {{ content: "[" counter(refs) "]"; position: absolute; left: 0; font-weight: 700; color: var(--azul); }}

    /* ── FOOTER ── */
    .footer {{
      background: var(--azul); color: rgba(255,255,255,.75);
      text-align: center; padding: 22px 20px; font-size: 13px; margin-top: 40px;
    }}
    .footer strong {{ color: #fff; }}

    /* ── PRE ── */
    pre {{ background: #0f1e2e; color: #c9e2f5; padding: 20px; border-radius: 8px;
           overflow-x: auto; font-size: 13px; line-height: 1.55; font-family: 'Consolas', 'Courier New', monospace; }}

    /* ── BADGE MÉTODO ── */
    .method-badge {{
      display: inline-block; background: var(--verde); color: #fff;
      font-size: 13px; font-weight: 700; padding: 4px 14px; border-radius: 20px;
      vertical-align: middle; margin-left: 10px;
    }}

    @media print {{
      .toc {{ display: none; }}
      .cover {{ break-after: page; }}
    }}
  </style>
</head>
<body>

<!-- ═══════════════════════════════════════════════════
     PORTADA INSTITUCIONAL
═══════════════════════════════════════════════════ -->
<div class="cover">
  <div class="cover-top">
    {logo_tag}
    <div class="cover-badge">GeomaticaPe Plugin v1.10</div>
  </div>
  <div class="cover-body">
    <div class="cover-pretitle">Reporte Técnico Científico</div>
    <div class="cover-title">Clasificación Supervisada<br>de Imágenes Multiespectrales</div>
    <div class="cover-meta">
      <div class="cover-meta-item">
        <span class="cover-meta-label">Algoritmo</span>
        <span class="cover-meta-value">{method_name}</span>
      </div>
      <div class="cover-meta-item">
        <span class="cover-meta-label">Validación</span>
        <span class="cover-meta-value">{val_method_name}</span>
      </div>
      <div class="cover-meta-item">
        <span class="cover-meta-label">Iteraciones (Folds)</span>
        <span class="cover-meta-value">{n_folds}</span>
      </div>
      <div class="cover-meta-item">
        <span class="cover-meta-label">Fecha de procesamiento</span>
        <span class="cover-meta-value">{fecha_str}</span>
      </div>
      <div class="cover-meta-item">
        <span class="cover-meta-label">Overall Accuracy</span>
        <span class="cover-meta-value">{oa * 100:.2f}%</span>
      </div>
      <div class="cover-meta-item">
        <span class="cover-meta-label">Kappa</span>
        <span class="cover-meta-value">{kappa:.4f} ({nivel_kappa})</span>
      </div>
    </div>
  </div>
</div>

<!-- TOC -->
<div class="toc">
  <div class="toc-title">Contenido del Reporte</div>
  <ul class="toc-list">
    <li><a href="#s1">1. Resumen Ejecutivo</a></li>
    <li><a href="#s2">2. Objetivo</a></li>
    <li><a href="#s3">3. Datos Utilizados</a></li>
    <li><a href="#s4">4. Metodología</a></li>
    <li><a href="#s5">5. Configuración del Modelo</a></li>
    <li><a href="#s6">6. Resultados y Métricas</a></li>
    <li><a href="#s7">7. Métricas por Clase</a></li>
    <li><a href="#s8">8. Importancia de Variables</a></li>
    <li><a href="#s9">9. Análisis Visual</a></li>
    <li><a href="#s10">10. Interpretación Técnica</a></li>
    <li><a href="#s11">11. Conclusiones</a></li>
    <li><a href="#s12">12. Recomendaciones</a></li>
    <li><a href="#s13">13. Trazabilidad y Bibliografía</a></li>
  </ul>
</div>

<div class="main">

<!-- ─── 1. RESUMEN EJECUTIVO ─── -->
<div class="section" id="s1">
  <div class="section-header">
    <div class="section-num">1</div>
    <div class="section-title">Resumen Ejecutivo</div>
  </div>
  <div class="card card-blue">
    <p>Se realizó una clasificación supervisada de imagen multiespectral mediante el algoritmo
    <strong>{method_name}</strong>, utilizando {n_poligonos} polígonos ROI distribuidos en
    <strong>{n_clases} clases</strong> de cobertura ({clases_nombres}).
    El modelo fue entrenado y validado con <strong>{n_pixeles:,} píxeles espectralmente válidos</strong>
    mediante el método de validación <em>{val_method_name}</em> con {n_folds} iteraciones.</p>
    <br>
    <p>Los resultados indican una <strong>exactitud global de {oa * 100:.2f}%</strong> y un
    <strong>índice Kappa de {kappa:.4f}</strong>, lo que corresponde a un nivel de acuerdo
    <em>{nivel_kappa}</em> según la escala de Landis &amp; Koch (1977). El modelo presenta un
    F1-Score macro de {f1m:.4f}, lo que refleja el rendimiento promedio no ponderado entre clases.
    Los archivos CSV de métricas, matriz de confusión e importancia de variables fueron exportados
    automáticamente junto con el raster clasificado y el vector correspondiente.</p>
  </div>
</div>

<!-- ─── 2. OBJETIVO ─── -->
<div class="section" id="s2">
  <div class="section-header">
    <div class="section-num">2</div>
    <div class="section-title">Objetivo del Análisis</div>
  </div>
  <div class="card">
    <p>Clasificar supervisadamente una imagen multiespectral identificando y delimitando
    espacialmente las clases de cobertura o uso del suelo definidas mediante polígonos de
    entrenamiento (ROI), evaluando la precisión del modelo con métricas estadísticas estándar
    y generando productos cartográficos y reportes técnicos reproducibles.</p>
  </div>
</div>

<!-- ─── 3. DATOS UTILIZADOS ─── -->
<div class="section" id="s3">
  <div class="section-header">
    <div class="section-num">3</div>
    <div class="section-title">Descripción de los Datos Utilizados</div>
  </div>
  <div class="info-grid">
    <div class="info-item">
      <div class="info-label">Imagen Multiespectral</div>
      <div class="info-value">{raster_name}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Shapefile ROI</div>
      <div class="info-value">{roi_name}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Número de Bandas</div>
      <div class="info-value">{len(band_names)}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Bandas / Variables</div>
      <div class="info-value">{", ".join(band_names)}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Polígonos ROI</div>
      <div class="info-value">{n_poligonos}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Píxeles de Entrenamiento</div>
      <div class="info-value">{n_pixeles:,}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Número de Clases</div>
      <div class="info-value">{n_clases}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Clases Definidas</div>
      <div class="info-value">{clases_nombres}</div>
    </div>
  </div>
</div>

<!-- ─── 4. METODOLOGÍA ─── -->
<div class="section" id="s4">
  <div class="section-header">
    <div class="section-num">4</div>
    <div class="section-title">Metodología Aplicada</div>
  </div>
  <div class="card">
    <p class="card-title">Método de Validación: {val_method_name}</p>
    <p>{metodologia_desc}</p>
    <div class="interp">
      <strong>Nota metodológica:</strong> En métodos de validación cruzada con múltiples iteraciones,
      la matriz de confusión reportada es la suma acumulada de todos los folds, y las métricas globales
      (OA, Kappa, F1) se calculan sobre el conjunto completo de predicciones acumuladas, garantizando
      consistencia estadística entre la matriz y los indicadores reportados.
    </div>
  </div>
</div>

<!-- ─── 5. CONFIGURACIÓN DEL MODELO ─── -->
<div class="section" id="s5">
  <div class="section-header">
    <div class="section-num">5</div>
    <div class="section-title">Configuración del Modelo de Clasificación</div>
  </div>
  <div class="info-grid">
    <div class="info-item">
      <div class="info-label">Algoritmo</div>
      <div class="info-value">{method_name}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Validación</div>
      <div class="info-value">{val_method_name}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Iteraciones / Folds</div>
      <div class="info-value">{n_folds}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Semilla (random_state)</div>
      <div class="info-value">{seed}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Fecha de procesamiento</div>
      <div class="info-value">{fecha_str}</div>
    </div>
    <div class="info-item">
      <div class="info-label">Plugin</div>
      <div class="info-value">GeomaticaPe v1.10</div>
    </div>
  </div>
</div>

<!-- ─── 6. RESULTADOS Y MÉTRICAS GLOBALES ─── -->
<div class="section" id="s6">
  <div class="section-header">
    <div class="section-num">6</div>
    <div class="section-title">Resultados Obtenidos – Métricas Globales
      <span class="method-badge">{method_name}</span>
    </div>
  </div>
  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-label">Overall Accuracy</div>
      <div class="kpi-val">{oa * 100:.2f}%</div>
      <div class="kpi-badge {'warn' if oa < 0.80 else 'danger' if oa < 0.70 else ''}">{nivel_oa}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Índice Kappa</div>
      <div class="kpi-val">{kappa:.4f}</div>
      <div class="kpi-badge {'warn' if kappa < 0.60 else 'danger' if kappa < 0.40 else ''}">{nivel_kappa}</div>
    </div>
    <div class="kpi green">
      <div class="kpi-label">F1 Macro</div>
      <div class="kpi-val">{f1m:.4f}</div>
      <div class="kpi-badge">Promedio no ponderado</div>
    </div>
    <div class="kpi green">
      <div class="kpi-label">F1 Weighted</div>
      <div class="kpi-val">{f1w:.4f}</div>
      <div class="kpi-badge">Ponderado por soporte</div>
    </div>
    <div class="kpi green">
      <div class="kpi-label">F1 Micro</div>
      <div class="kpi-val">{f1mi:.4f}</div>
      <div class="kpi-badge">Global acumulado</div>
    </div>
  </div>
</div>

<!-- ─── 7. MÉTRICAS POR CLASE ─── -->
<div class="section" id="s7">
  <div class="section-header">
    <div class="section-num">7</div>
    <div class="section-title">Métricas de Validación por Clase</div>
  </div>
  <div class="tbl-wrap">
    <table>
      <thead>
        <tr>
          <th>Clase</th>
          <th>Precisión</th>
          <th>Sensibilidad (Recall)</th>
          <th>Especificidad</th>
          <th>F1-Score</th>
          <th>Soporte (px)</th>
        </tr>
      </thead>
      <tbody>
        {tabla_clases_rows}
      </tbody>
    </table>
  </div>
  <div class="interp">
    <strong>Lectura de la tabla:</strong>
    <strong>Precisión</strong> (exactitud de las predicciones positivas) –
    <strong>Sensibilidad/Recall</strong> (capacidad de detectar todos los casos reales) –
    <strong>Especificidad</strong> (capacidad de rechazar correctamente las clases negativas) –
    <strong>F1-Score</strong> (media armónica de Precisión y Recall).
    Valores ≥0.90 en verde oscuro, ≥0.80 en verde, ≥0.70 en verde claro, ≥0.60 en amarillo, &lt;0.60 en rojo.
  </div>
</div>

<!-- ─── 8. IMPORTANCIA DE VARIABLES ─── -->
<div class="section" id="s8">
  <div class="section-header">
    <div class="section-num">8</div>
    <div class="section-title">Importancia de Bandas / Variables Predictoras</div>
  </div>
  <div class="tbl-wrap" style="margin-bottom:20px;">
    <table>
      <thead>
        <tr>
          <th style="text-align:center;">Rango</th>
          <th>Banda / Variable</th>
          <th>Importancia Relativa</th>
        </tr>
      </thead>
      <tbody>
        {tabla_imp_rows}
      </tbody>
    </table>
  </div>
  <div class="interp">{imp_interp}</div>
</div>

<!-- ─── 9. ANÁLISIS VISUAL ─── -->
<div class="section" id="s9">
  <div class="section-header">
    <div class="section-num">9</div>
    <div class="section-title">Presentación Gráfica de Resultados</div>
  </div>
  <div class="charts-grid">
    <div class="chart-box">
      <h4>Matriz de Confusión (acumulada)</h4>
      <img src="data:image/png;base64,{img_cm_b64}" alt="Matriz de Confusión">
    </div>
    <div class="chart-box">
      <h4>Métricas por Clase (Heatmap)</h4>
      <img src="data:image/png;base64,{img_heatmap_b64}" alt="Heatmap de métricas">
    </div>
  </div>
</div>

<!-- ─── 10. INTERPRETACIÓN TÉCNICA ─── -->
<div class="section" id="s10">
  <div class="section-header">
    <div class="section-num">10</div>
    <div class="section-title">Interpretación Técnica de los Resultados</div>
  </div>
  <div class="card card-blue">
    <p class="card-title">Exactitud Global (OA)</p>
    <p>{interp_oa}</p>
  </div>
  <div class="card">
    <p class="card-title">Índice Kappa de Cohen</p>
    <p>{interp_kappa}</p>
  </div>
  <div class="card card-blue">
    <p class="card-title">Importancia de Variables</p>
    <p>{imp_interp}</p>
  </div>
  <div class="card">
    <p class="card-title">Matriz de Confusión</p>
    <p>La matriz de confusión acumulada sobre {n_folds} fold(s) permite identificar los patrones
    de error sistemático entre clases. Los elementos fuera de la diagonal principal representan
    confusiones entre categorías, frecuentemente asociadas a similitud espectral, mezcla de píxeles
    en bordes (efecto borde), o heterogeneidad interna de las clases definidas. Las celdas con
    valores elevados fuera de la diagonal deben guiar la revisión del diseño muestral.</p>
  </div>
</div>

<!-- ─── 11. CONCLUSIONES ─── -->
<div class="section" id="s11">
  <div class="section-header">
    <div class="section-num">11</div>
    <div class="section-title">Conclusiones</div>
  </div>
  <div class="card">
    <ul style="padding-left:20px;line-height:2;">
      <li>El algoritmo <strong>{method_name}</strong> logró clasificar la imagen multiespectral
          con una exactitud global de <strong>{oa * 100:.2f}%</strong> y un índice Kappa de
          <strong>{kappa:.4f}</strong> ({nivel_kappa}).</li>
      <li>El método de validación <strong>{val_method_name}</strong> con <strong>{n_folds} iteración(es)</strong>
          proporcionó una estimación {'robusta y estadísticamente representativa' if n_folds > 1 else 'basada en una sola partición'} del error de generalización del modelo.</li>
      <li>La variable de mayor contribución al modelo fue <strong>{top_band}</strong>
          ({top_pct:.1f}% de importancia relativa), lo que orienta futuros análisis sobre
          la relevancia de cada banda espectral.</li>
      <li>Los productos generados (raster clasificado, vector, CSVs de métricas e importancia,
          y reporte técnico) permiten la trazabilidad completa del análisis.</li>
    </ul>
  </div>
</div>

<!-- ─── 12. RECOMENDACIONES ─── -->
<div class="section" id="s12">
  <div class="section-header">
    <div class="section-num">12</div>
    <div class="section-title">Recomendaciones</div>
  </div>
  <div class="card">
    <ul style="padding-left:20px;line-height:2;">
      <li>{'Validar los resultados con datos de campo adicionales e independientes, ya que la exactitud ≥80% sugiere un modelo confiable pero siempre sujeto a verificación en campo.' if oa >= 0.80 else 'Incrementar el número y calidad de los polígonos ROI, especialmente para las clases con métricas más bajas, con el objetivo de superar el umbral de 80% de exactitud global.'}</li>
      <li>Evaluar la separabilidad espectral entre clases mediante índices como la Distancia de Jeffries-Matusita antes de agregar nuevas categorías de cobertura.</li>
      <li>Considerar la incorporación de variables auxiliares (DEM, índices espectrales, textura)
          para mejorar la discriminación de clases con baja precisión individual.</li>
      <li>{'Comparar el rendimiento con otros algoritmos disponibles en la herramienta (RF, XGBoost, SVM) para seleccionar el modelo óptimo para este conjunto de datos.' if 'Random Forest' not in method_name else 'Probar variantes del modelo ajustando hiperparámetros (n_estimators, max_depth) para potencialmente mejorar la exactitud.'}</li>
      <li>Para publicaciones científicas, utilizar métodos de validación espacial (Spatial Cross Validation
          o Spatial Block CV) que controlan la autocorrelación espacial y producen estimaciones de error más conservadoras y realistas.</li>
    </ul>
  </div>
</div>

<!-- ─── 13. TRAZABILIDAD Y BIBLIOGRAFÍA ─── -->
<div class="section" id="s13">
  <div class="section-header">
    <div class="section-num">13</div>
    <div class="section-title">Trazabilidad Científica y Bibliografía</div>
  </div>
  <div class="card card-blue" style="margin-bottom:20px;">
    <p class="card-title">Parámetros de Procesamiento</p>
    <div class="info-grid">
      <div class="info-item"><div class="info-label">Herramienta</div><div class="info-value">GeomaticaPe v1.10</div></div>
      <div class="info-item"><div class="info-label">Algoritmo</div><div class="info-value">{method_name}</div></div>
      <div class="info-item"><div class="info-label">Validación</div><div class="info-value">{val_method_name} ({n_folds} fold/s)</div></div>
      <div class="info-item"><div class="info-label">Semilla</div><div class="info-value">{seed}</div></div>
      <div class="info-item"><div class="info-label">Fecha</div><div class="info-value">{fecha_str}</div></div>
      <div class="info-item"><div class="info-label">Imagen</div><div class="info-value">{raster_name}</div></div>
      <div class="info-item"><div class="info-label">ROI</div><div class="info-value">{roi_name}</div></div>
      <div class="info-item"><div class="info-label">Píxeles totales</div><div class="info-value">{n_pixeles:,}</div></div>
    </div>
  </div>

  <div class="card">
    <p class="card-title">Reporte Estadístico Completo (classification_report)</p>
    <pre>{report_text}</pre>
  </div>

  <div class="card">
    <p class="card-title">Referencias Bibliográficas</p>
    <ul class="ref-list">
      <li>Anderson, J. R., Hardy, E. E., Roach, J. T., &amp; Witmer, R. E. (1976). <em>A land use and land cover classification system for use with remote sensor data</em>. USGS Professional Paper 964. U.S. Government Printing Office.</li>
      <li>Congalton, R. G., &amp; Green, K. (2019). <em>Assessing the Accuracy of Remotely Sensed Data: Principles and Practices</em> (3rd ed.). CRC Press.</li>
      <li>Landis, J. R., &amp; Koch, G. G. (1977). The measurement of observer agreement for categorical data. <em>Biometrics</em>, 33(1), 159–174.</li>
      <li>Pedregosa, F., et al. (2011). Scikit-learn: Machine learning in Python. <em>Journal of Machine Learning Research</em>, 12, 2825–2830.</li>
      <li>Breiman, L. (2001). Random Forests. <em>Machine Learning</em>, 45(1), 5–32.</li>
      <li>Chen, T., &amp; Guestrin, C. (2016). XGBoost: A Scalable Tree Boosting System. <em>Proceedings of KDD '16</em>, 785–794.</li>
      <li>Roberts, D. R., et al. (2017). Cross-validation strategies for data with temporal, spatial, hierarchical, or phylogenetic structure. <em>Ecography</em>, 40(8), 913–929.</li>
      <li>Cortes, C., &amp; Vapnik, V. (1995). Support-vector networks. <em>Machine Learning</em>, 20(3), 273–297.</li>
    </ul>
  </div>
</div>

</div><!-- /main -->

<div class="footer">
  <strong>Geomatica Ambiental</strong> &nbsp;|&nbsp; geomatica.pe &nbsp;|&nbsp;
  Reporte generado automáticamente por GeomaticaPe Plugin v1.10 &nbsp;|&nbsp; {fecha_str}
</div>

</body>
</html>
"""
    ruta_html = os.path.join(out_dir, "reporte_clasificacion.html")
    with open(ruta_html, "w", encoding="utf-8") as fh:
        fh.write(html)
    return ruta_html

# =========================================================================


class ClasificacionSupervisada(GeomaticapeAlgorithm):
    _algorithm_name = "clasificacion_supervisada"
    _icon_name = "clasif_supervisada.png"

    INPUT_RASTER = "INPUT_RASTER"
    INPUT_ROI = "INPUT_ROI"
    FIELD_ID = "FIELD_ID"
    FIELD_CLASS = "FIELD_CLASS"
    VAL_METHOD = "VAL_METHOD"
    PCT_VAL = "PCT_VAL"
    K_VALUE = "K_VALUE"
    METHOD = "METHOD"
    SEED = "SEED"
    OUT_FOLDER = "OUT_FOLDER"

    def __init__(self):
        super().__init__()
        self._method_keys = [
            "Gaussian Mixture Model (GMM)",
            "Random Forest (RF)",
            "Support Vector Machine (SVM)",
            "K-Nearest Neighbors (KNN)",
            "XGBoost (XGB)",
            "CatBoost (CB)",
            "Extra Trees (ET)",
            "Gradient Boosting Classifier (GBC)",
            "Logistic Regression (LR)",
            "Gaussian Naive Bayes (NB)",
            "Multi-layer Perceptron (MLP)"
        ]
        self._val_methods = [
            "Hold-Out (70/30, 80/20...)",
            "K-Fold Cross Validation",
            "Stratified K-Fold",
            "Repeated K-Fold",
            "Leave-One-Out (LOOCV)",
            "Leave-P-Out",
            "Bootstrap",
            "Spatial Cross Validation",
            "Spatial Block Cross Validation",
            "Monte Carlo Cross Validation"
        ]

    def displayName(self):
        return self.tr("Clasificacion supervisada y validacion")

    def group(self):
        return self.tr("Procesamiento")

    def groupId(self):
        return "geomaticape_procesamiento"

    def shortHelpString(self):
        algos = ", ".join(self._method_keys)
        return f"""
<h3>Clasificacion supervisada y validacion Avanzada</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br>
<b>Version:</b> 1.10<br><br>

<b>Descripcion:</b><br>
Entrena un clasificador supervisado sobre cualquier imagen multiespectral
usando poligonos ROI. Soporta 10 metodos de validacion cruzada (espaciales y estadisticos).<br><br>

<b>Algoritmos disponibles:</b> {algos}.<br><br>

<b>Ayuda de Parámetros:</b><br>
<ul>
<li><b>Metodología de Validación:</b> Elige el método para evaluar la precisión del modelo (ej. K-Fold, Hold-Out, Espacial).</li>
<li><b>Valor de K (Folds) o P (Iteraciones):</b>
    <ul>
    <li>En <i>K-Fold</i> o <i>Stratified K-Fold</i>: Indica el número de pliegues o particiones.</li>
    <li>En <i>Spatial Block Cross Validation</i>: Indica el número de bloques espaciales (grilla nxn) en los que se dividirá el área de estudio.</li>
    <li>En <i>Repeated K-Fold</i> o <i>Bootstrap</i>: Indica el número de repeticiones.</li>
    </ul>
</li>
<li><b>% de Validación:</b> Porcentaje de datos reservados para validación (sólo aplica a Hold-Out y Monte Carlo).</li>
</ul>
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT_RASTER, self.tr("Imagen multiespectral")
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_ROI, self.tr("Shapefile ROI (poligonos)"),
            types=[QgsProcessing.SourceType.TypeVectorPolygon]
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_ID, self.tr("Campo ID de Polígono (entero, ej. 1,2,3)"),
            parentLayerParameterName=self.INPUT_ROI,
            defaultValue="ID"
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_CLASS, self.tr("Campo Clase (texto)"),
            parentLayerParameterName=self.INPUT_ROI,
            defaultValue="Clase"
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.VAL_METHOD, self.tr("Metodología de Validación"),
            options=self._val_methods,
            defaultValue=0, allowMultiple=False
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.PCT_VAL, self.tr(
                "% de Validacion (solo para Hold-Out y Monte Carlo)"),
            type=QgsProcessingParameterNumber.Type.Integer,
            defaultValue=30, minValue=5, maxValue=80
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.K_VALUE, self.tr("Valor de K (Folds) o P (Iteraciones)"),
            type=QgsProcessingParameterNumber.Type.Integer,
            defaultValue=5, minValue=2
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, self.tr("Algoritmo de clasificacion"),
            options=self._method_keys,
            defaultValue=1, allowMultiple=False
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.SEED, self.tr("Semilla (random_state)"),
            type=QgsProcessingParameterNumber.Type.Integer,
            defaultValue=42, minValue=0, maxValue=99999
        ))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUT_FOLDER, self.tr("Carpeta de salida")
        ))

    def processAlgorithm(self, parameters, context, feedback):
        try:
            from sklearn.model_selection import (
                train_test_split, KFold, StratifiedKFold, RepeatedKFold,
                LeaveOneGroupOut, LeavePGroupsOut, ShuffleSplit
            )
            from sklearn.cluster import KMeans
            from sklearn.metrics import classification_report, accuracy_score, f1_score
        except ImportError:
            _show_missing_library("scikit-learn", "pip install scikit-learn")

        rl = self.parameterAsRasterLayer(
            parameters, self.INPUT_RASTER, context)
        vl = self.parameterAsVectorLayer(parameters, self.INPUT_ROI, context)
        f_id = self.parameterAsString(parameters, self.FIELD_ID, context)
        f_cls = self.parameterAsString(parameters, self.FIELD_CLASS, context)
        val_idx = self.parameterAsEnum(parameters, self.VAL_METHOD, context)
        pct = self.parameterAsInt(parameters, self.PCT_VAL, context)
        k_val = self.parameterAsInt(parameters, self.K_VALUE, context)
        m_idx = self.parameterAsEnum(parameters, self.METHOD, context)
        seed = self.parameterAsInt(parameters, self.SEED, context)
        out_dir = self.parameterAsString(parameters, self.OUT_FOLDER, context)

        if rl is None:
            raise QgsProcessingException("No se cargo el raster.")
        if vl is None:
            raise QgsProcessingException("No se cargo el ROI.")

        method_name = self._method_keys[m_idx]
        val_method_name = self._val_methods[val_idx]

        # Generar diccionario descriptivo para el reporte HTML
        metodologia_text = {
            "Hold-Out (70/30, 80/20...)": f"Hold-Out ({100 - pct}/{pct}): Divide el dataset aleatoriamente en un conjunto de entrenamiento y uno de validación según el porcentaje especificado. Es rápido pero el resultado puede depender de la partición aleatoria específica.",
            "K-Fold Cross Validation": f"K-Fold Cross Validation: Divide aleatoriamente los datos en {k_val} particiones (folds) iguales. El modelo se entrena {k_val} veces, usando cada partición como validación una vez. La matriz reportada es el promedio general.",
            "Stratified K-Fold": f"Stratified K-Fold: Similar a K-Fold con {k_val} pliegues, pero garantiza que cada pliegue mantenga la misma proporción de clases que el dataset original, siendo ideal para clases desbalanceadas.",
            "Repeated K-Fold": f"Repeated K-Fold: Repite el proceso K-Fold {k_val} veces con diferentes aleatorizaciones (splits=5, repeats={k_val}), proporcionando una estimación muchísimo más robusta del error.",
            "Leave-One-Out (LOOCV)": "Leave-One-Out (LOOCV) a nivel Polígono: Excluye iterativamente un solo polígono del ROI para validación y entrena con el resto. Muy robusto espacialmente ya que evalúa la capacidad de predecir zonas completamente nuevas aisladas.",
            "Leave-P-Out": f"Leave-P-Out a nivel Polígono: Excluye iterativamente {k_val} polígonos del ROI simultáneamente para validación. Es una generalización estricta de LOOCV.",
            "Bootstrap": f"Bootstrap: Realiza {k_val} iteraciones creando conjuntos de entrenamiento mediante muestreo aleatorio con reemplazo del mismo tamaño que el original. Los píxeles no seleccionados forman la validación Out-Of-Bag.",
            "Spatial Cross Validation": f"Spatial Cross Validation (K-Means): Agrupa espacialmente las coordenadas en {k_val} clusters usando K-Means. Entrena iterativamente dejando un cluster espacial entero fuera para validación, minimizando fuertemente la autocorrelación espacial.",
            "Spatial Block Cross Validation": f"Spatial Block Cross Validation: Divide el área de estudio en una cuadrícula (grid) espacial {k_val}x{k_val}. Extrae bloques enteros para validación simulando predicciones en áreas no muestreadas.",
            "Monte Carlo Cross Validation": f"Monte Carlo Cross Validation: Selecciona {k_val} veces particiones aleatorias independientes usando el {pct}% de validación. A diferencia de K-Fold, las particiones de distintas iteraciones pueden solaparse."
        }

        # Factory of models
        if method_name == "Gaussian Mixture Model (GMM)":
            from sklearn.mixture import GaussianMixture

            class GMMClassifier:
                def __init__(self, seed):
                    self.seed = seed
                    self.models = {}

                def fit(self, X, y):
                    self.classes = np.unique(y)
                    for c in self.classes:
                        Xc = X[y == c]
                        gmm = GaussianMixture(
                            n_components=1, random_state=self.seed)
                        if len(Xc) > 0:
                            gmm.fit(Xc)
                        self.models[c] = gmm
                    return self

                def predict(self, X):
                    scores = np.zeros((X.shape[0], len(self.classes)))
                    for i, c in enumerate(self.classes):
                        if hasattr(self.models[c], 'precisions_cholesky_'):
                            scores[:, i] = self.models[c].score_samples(X)
                        else:
                            scores[:, i] = -np.inf
                    return self.classes[np.argmax(scores, axis=1)]

            def model_factory(s): return GMMClassifier(s)

        elif method_name == "Random Forest (RF)":
            from sklearn.ensemble import RandomForestClassifier
            def model_factory(s): return RandomForestClassifier(
                n_estimators=100, n_jobs=-1, random_state=s)

        elif method_name == "Support Vector Machine (SVM)":
            from sklearn.svm import SVC
            def model_factory(s): return SVC(random_state=s, probability=False)

        elif method_name == "K-Nearest Neighbors (KNN)":
            from sklearn.neighbors import KNeighborsClassifier

            def model_factory(s): return KNeighborsClassifier(
                n_neighbors=5, n_jobs=-1)

        elif method_name == "XGBoost (XGB)":
            try:
                pass
            except ImportError:
                _show_missing_library("xgboost", "pip install xgboost")

            class XGBWrapper:
                def __init__(self, seed):
                    self.seed = seed
                    self.model = None
                    self.le = None

                def fit(self, X, y):
                    from sklearn.preprocessing import LabelEncoder
                    import xgboost as xgb
                    self.le = LabelEncoder()
                    y_enc = self.le.fit_transform(y)
                    self.model = xgb.XGBClassifier(
                        random_state=self.seed, eval_metric='mlogloss', n_jobs=-1)
                    self.model.fit(X, y_enc)
                    return self

                def predict(self, X):
                    preds = self.model.predict(X)
                    return self.le.inverse_transform(preds)

            def model_factory(s): return XGBWrapper(s)

        elif method_name == "CatBoost (CB)":
            try:
                pass
            except ImportError:
                _show_missing_library("catboost", "pip install catboost")

            class CBWrapper:
                def __init__(self, seed):
                    self.seed = seed
                    self.model = None
                    self.le = None

                def fit(self, X, y):
                    import catboost as cb
                    from sklearn.preprocessing import LabelEncoder
                    # LabelEncoder para manejar clases no numéricas o no
                    # contiguas
                    self.le = LabelEncoder()
                    y_enc = self.le.fit_transform(y)
                    self.model = cb.CatBoostClassifier(
                        random_seed=self.seed, verbose=False, thread_count=-1)
                    self.model.fit(X, y_enc)
                    return self

                def predict(self, X):
                    preds = self.model.predict(X)
                    if len(preds.shape) > 1:
                        preds = preds.flatten()
                    # Devolver clases originales, no índices codificados
                    return self.le.inverse_transform(preds.astype(int))

            def model_factory(s): return CBWrapper(s)

        elif method_name == "Extra Trees (ET)":
            from sklearn.ensemble import ExtraTreesClassifier

            def model_factory(s): return ExtraTreesClassifier(
                n_estimators=100, n_jobs=-1, random_state=s)

        elif method_name == "Gradient Boosting Classifier (GBC)":
            from sklearn.ensemble import GradientBoostingClassifier

            def model_factory(s): return GradientBoostingClassifier(
                random_state=s)

        elif method_name == "Logistic Regression (LR)":
            from sklearn.linear_model import LogisticRegression
            def model_factory(s): return LogisticRegression(
                random_state=s, max_iter=1000, n_jobs=-1)

        elif method_name == "Gaussian Naive Bayes (NB)":
            from sklearn.naive_bayes import GaussianNB
            def model_factory(s): return GaussianNB()

        elif method_name == "Multi-layer Perceptron (MLP)":
            from sklearn.neural_network import MLPClassifier
            def model_factory(s): return MLPClassifier(
                hidden_layer_sizes=(100, 50), max_iter=500, random_state=s)

        else:
            raise QgsProcessingException(
                f"Metodo no disponible: {method_name}")

        os.makedirs(out_dir, exist_ok=True)
        raster_path = rl.source()
        info = _info_raster(raster_path)

        feedback.pushInfo(
            "====================================================")
        feedback.pushInfo("Clasificacion supervisada y validacion Avanzada")
        feedback.pushInfo(f"Raster   : {os.path.basename(raster_path)}")
        feedback.pushInfo(
            f"ROI      : {
                vl.name()} ({
                vl.featureCount()} poligonos)")
        feedback.pushInfo(f"Algoritmo: {method_name}")
        feedback.pushInfo(f"Validacion: {val_method_name}")
        feedback.pushInfo(
            "====================================================")

        crs_r = QgsCoordinateReferenceSystem()
        crs_r.createFromWkt(info["proj"])
        if not crs_r.isValid():
            crs_r = rl.crs()

        if vl.crs().authid() != crs_r.authid():
            res = processing.run(
                "native:reprojectlayer",
                {"INPUT": vl, "TARGET_CRS": crs_r, "OUTPUT": "memory:"},
                context=context, feedback=feedback
            )
            roi_layer = res["OUTPUT"]
        else:
            roi_layer = vl

        # Crear shape temporal y añadir campo especial __FID__ para LOOCV por
        # Poligono
        tmp_roi = os.path.join(out_dir, "_tmp_roi_proj.shp")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "ESRI Shapefile"
        opts.fileEncoding = "UTF-8"
        QgsVectorFileWriter.writeAsVectorFormatV3(
            roi_layer, tmp_roi, context.transformContext(), opts)

        layer_tmp = ogr.Open(tmp_roi, 1)
        lyr = layer_tmp.GetLayer()
        lyr.CreateField(ogr.FieldDefn("__FID__", ogr.OFTInteger))
        for feat in lyr:
            feat.SetField("__FID__", feat.GetFID() + 1)  # Asegurar > 0
            lyr.SetFeature(feat)
        layer_tmp = None

        id2cls = {}
        for ft in roi_layer.getFeatures():
            try:
                ide = int(ft[f_id])
            except Exception as e:
                feedback.pushInfo(
                    f"Aviso: se omite feature {ft.id()} con id '{f_id}' "
                    f"no numerico: {e}")
                continue
            cls = ft[f_cls]
            if ide not in id2cls and cls not in (None, ""):
                id2cls[ide] = str(cls)

        rast_id = os.path.join(out_dir, "_tmp_roi_id.tif")
        _rasterizar_roi(tmp_roi, info, f_id, rast_id)

        rast_fid = os.path.join(out_dir, "_tmp_roi_fid.tif")
        _rasterizar_roi(tmp_roi, info, "__FID__", rast_fid)

        ds_mask = gdal.Open(rast_id, gdal.GA_ReadOnly)
        mask = ds_mask.GetRasterBand(1).ReadAsArray()
        ds_mask = None

        ds_fid_mask = gdal.Open(rast_fid, gdal.GA_ReadOnly)
        fid_mask = ds_fid_mask.GetRasterBand(1).ReadAsArray()
        ds_fid_mask = None

        idx_train = mask != -9999
        n_train_total = int(idx_train.sum())
        if n_train_total == 0:
            raise QgsProcessingException(
                "No se obtuvieron pixeles del ROI válidos.")

        ds = gdal.Open(raster_path, gdal.GA_ReadOnly)
        chunk_size = 2048
        cols = info["cols"]
        rows = info["rows"]
        gt = info["gt"]

        X_list = []
        y_list = []
        fid_list = []
        xy_list = []

        for yoff in range(0, rows, chunk_size):
            if feedback.isCanceled():
                break
            ysize = min(chunk_size, rows - yoff)
            for xoff in range(0, cols, chunk_size):
                xsize = min(chunk_size, cols - xoff)

                mask_chunk = mask[yoff:yoff + ysize, xoff:xoff + xsize]
                idx_chunk = mask_chunk != -9999

                if not np.any(idx_chunk):
                    continue

                fid_chunk = fid_mask[yoff:yoff + ysize, xoff:xoff + xsize]

                chunk_data = []
                for b in range(1, info["nbands"] + 1):
                    arr = ds.GetRasterBand(b).ReadAsArray(
                        xoff, yoff, xsize, ysize).astype(np.float32)
                    nd = info["nodata"][b - 1]
                    if nd is not None:
                        arr[arr == nd] = np.nan
                    chunk_data.append(arr[idx_chunk])

                X_chunk = np.column_stack(chunk_data)
                y_chunk = mask_chunk[idx_chunk].astype(np.int64)
                fid_chunk = fid_chunk[idx_chunk].astype(np.int64)

                rows_c, cols_c = np.where(idx_chunk)
                xgeo_c = gt[0] + (cols_c + xoff + 0.5) * \
                    gt[1] + (rows_c + yoff + 0.5) * gt[2]
                ygeo_c = gt[3] + (cols_c + xoff + 0.5) * \
                    gt[4] + (rows_c + yoff + 0.5) * gt[5]
                xy_chunk = np.column_stack([xgeo_c, ygeo_c])

                ok_chunk = np.isfinite(X_chunk).all(axis=1)
                X_list.append(X_chunk[ok_chunk])
                y_list.append(y_chunk[ok_chunk])
                fid_list.append(fid_chunk[ok_chunk])
                xy_list.append(xy_chunk[ok_chunk])

        if not X_list:
            raise QgsProcessingException(
                "No se obtuvieron pixeles validos despues de filtrar NaNs.")

        X_full = np.vstack(X_list)
        y_full = np.concatenate(y_list)
        fid_full = np.concatenate(fid_list)
        xy_full = np.vstack(xy_list)

        del X_list, y_list, fid_list, xy_list
        gc.collect()

        feedback.pushInfo(f"Muestras validas totales: {len(y_full):,}")
        feedback.setProgress(20)

        # Configuracion de Validacion Cruzada
        cv_splits = []
        groups = None

        if val_method_name == "Hold-Out (70/30, 80/20...)":
            try:
                tr_i, te_i = train_test_split(
                    np.arange(
                        len(y_full)), test_size=pct / 100.0, random_state=seed, stratify=y_full)
            except ValueError:
                tr_i, te_i = train_test_split(
                    np.arange(len(y_full)), test_size=pct / 100.0, random_state=seed)
            cv_splits.append((tr_i, te_i))

        elif val_method_name == "K-Fold Cross Validation":
            kf = KFold(n_splits=k_val, shuffle=True, random_state=seed)
            cv_splits = list(kf.split(X_full))

        elif val_method_name == "Stratified K-Fold":
            skf = StratifiedKFold(
                n_splits=k_val,
                shuffle=True,
                random_state=seed)
            try:
                cv_splits = list(skf.split(X_full, y_full))
            except ValueError:
                feedback.pushInfo(
                    "Aviso: No hay suficientes muestras de alguna clase para Stratified K-Fold. Usando K-Fold regular.")
                kf = KFold(n_splits=k_val, shuffle=True, random_state=seed)
                cv_splits = list(kf.split(X_full))

        elif val_method_name == "Repeated K-Fold":
            rkf = RepeatedKFold(n_splits=5, n_repeats=k_val, random_state=seed)
            cv_splits = list(rkf.split(X_full))

        elif val_method_name == "Leave-One-Out (LOOCV)":
            logo = LeaveOneGroupOut()
            cv_splits = list(logo.split(X_full, y_full, fid_full))

        elif val_method_name == "Leave-P-Out":
            lpgo = LeavePGroupsOut(n_groups=k_val)
            # Para evitar MemoryError o demoras infinitas, limitaremos a máx
            # combinaciones
            try:
                cv_splits = list(lpgo.split(X_full, y_full, fid_full))
                if len(cv_splits) > 500:
                    feedback.pushInfo(
                        f"Aviso: Leave-P-Out generó {len(cv_splits)} iteraciones. Limitando a 500 aleatorias.")
                    # Muestreo reproducible sin tocar el estado global del
                    # módulo random (no es un uso criptográfico: solo se
                    # busca reducir el número de combinaciones a evaluar).
                    rng = np.random.RandomState(seed)
                    sel_idx = rng.choice(
                        len(cv_splits), size=500, replace=False)
                    cv_splits = [cv_splits[i] for i in sel_idx]
            except Exception as e:
                raise QgsProcessingException(f"Error en Leave-P-Out: {str(e)}")

        elif val_method_name == "Bootstrap":
            rs = np.random.RandomState(seed)
            n = len(y_full)
            for _ in range(k_val):
                tr_i = rs.choice(n, size=n, replace=True)
                te_i = np.setdiff1d(np.arange(n), tr_i)
                if len(te_i) > 0:
                    cv_splits.append((tr_i, te_i))

        elif val_method_name == "Spatial Cross Validation":
            km = KMeans(n_clusters=k_val, random_state=seed)
            sp_groups = km.fit_predict(xy_full)
            logo = LeaveOneGroupOut()
            cv_splits = list(logo.split(X_full, y_full, sp_groups))

        elif val_method_name == "Spatial Block Cross Validation":
            xmin, xmax = xy_full[:, 0].min(), xy_full[:, 0].max()
            ymin, ymax = xy_full[:, 1].min(), xy_full[:, 1].max()
            dx = (xmax - xmin) / k_val + 1e-6
            dy = (ymax - ymin) / k_val + 1e-6
            col = ((xy_full[:, 0] - xmin) / dx).astype(int)
            row = ((xy_full[:, 1] - ymin) / dy).astype(int)
            sp_block_groups = row * k_val + col
            n_bloques_efectivos = len(np.unique(sp_block_groups))
            feedback.pushInfo(
                f"Spatial Block CV: se creó una grilla {k_val}x{k_val} "
                f"({k_val * k_val} bloques posibles). "
                f"Bloques con muestras ROI: {n_bloques_efectivos} "
                f"(= número real de folds de validación)."
            )
            if n_bloques_efectivos < 2:
                raise QgsProcessingException(
                    "Spatial Block CV: menos de 2 bloques contienen muestras. "
                    "Reduzca el valor de K o use un método de validación diferente."
                )
            logo = LeaveOneGroupOut()
            cv_splits = list(logo.split(X_full, y_full, sp_block_groups))

        elif val_method_name == "Monte Carlo Cross Validation":
            ss = ShuffleSplit(
                n_splits=k_val,
                test_size=pct / 100.0,
                random_state=seed)
            cv_splits = list(ss.split(X_full))

        if not cv_splits:
            raise QgsProcessingException(
                "La metodologia seleccionada no genero particiones de validacion validas.")

        feedback.pushInfo(
            f"Total de iteraciones de validacion (Pliegues): {
                len(cv_splits)}")

        # Guardar Shapefiles usando el PRIMER fold como ejemplo representativo.
        # NOTA: Para métodos multi-fold (K-Fold, Bootstrap, LOOCV, etc.), estos
        # shapefiles representan UNA sola partición, no la validación completa.
        # Las métricas del reporte sí usan todos los folds acumulados.
        tr_i, te_i = cv_splits[0]
        feedback.pushInfo(
            f"Shapefiles roi_train / roi_test: generados con el fold 1 de {
                len(cv_splits)} "
            f"({len(tr_i):,} pixeles entrenamiento, {len(te_i):,} pixeles validación). "
            f"Las métricas del reporte usan todos los folds acumulados."
        )
        X_tr_ex, X_te_ex = X_full[tr_i], X_full[te_i]
        y_tr_ex, y_te_ex = y_full[tr_i], y_full[te_i]
        xy_tr_ex, xy_te_ex = xy_full[tr_i], xy_full[te_i]

        band_field_names = [_safe_field(nm) for nm in info["band_names"]]
        seen = set()
        out_names = []
        for nm in band_field_names:
            base = nm
            k = 1
            while nm in seen:
                nm = (base[:8] + f"_{k}")[:10]
                k += 1
            seen.add(nm)
            out_names.append(nm)
        band_field_names = out_names

        cols_attr = ["ID", "Clase"] + band_field_names
        tipos_attr = [QVariant.Int, QVariant.String] + \
            [QVariant.Double] * info["nbands"]

        def _filas(X, y, XY):
            for i in range(len(y)):
                ide = int(y[i])
                cls = id2cls.get(ide, str(ide))
                yield (float(XY[i, 0]), float(XY[i, 1]), ide, cls, *[float(v) for v in X[i]])

        train_shp = os.path.join(out_dir, "roi_train.shp")
        test_shp = os.path.join(out_dir, "roi_test.shp")
        _escribir_shp_puntos(
            train_shp,
            gt,
            info["proj"],
            list(
                _filas(
                    X_tr_ex,
                    y_tr_ex,
                    xy_tr_ex)),
            cols_attr,
            tipos_attr)
        _escribir_shp_puntos(
            test_shp,
            gt,
            info["proj"],
            list(
                _filas(
                    X_te_ex,
                    y_te_ex,
                    xy_te_ex)),
            cols_attr,
            tipos_attr)
        feedback.setProgress(30)

        # Entrenar iterativamente
        feedback.pushInfo(
            f"Iniciando ciclo de entrenamiento y validación ({method_name})...")
        confusion_matrices = []
        all_y_true = []
        all_y_pred = []

        global_clases_id = sorted(np.unique(y_full).tolist())

        for fold, (tr_idx, te_idx) in enumerate(cv_splits, 1):
            if fold % 5 == 0 or fold == len(cv_splits) or fold == 1:
                feedback.pushInfo(
                    f" -> Procesando iteracion {fold}/{len(cv_splits)}...")

            X_tr, X_te = X_full[tr_idx], X_full[te_idx]
            y_tr, y_te = y_full[tr_idx], y_full[te_idx]

            # Semilla derivada por fold: reproducible con la misma semilla base
            # pero con varianza interna honesta entre iteraciones
            fold_seed = seed + fold
            model = model_factory(fold_seed)
            model.fit(X_tr, y_tr)
            y_pred = model.predict(X_te)

            M = _matriz_confusion(y_te, y_pred, global_clases_id)
            confusion_matrices.append(M)
            all_y_true.extend(y_te)
            all_y_pred.extend(y_pred)

        feedback.setProgress(60)

        # Calculo de Matriz de Confusion Final
        # Se usa la SUMA (no el promedio) para que sea consistente con las métricas
        # globales calculadas sobre all_y_true / all_y_pred acumulados.
        # El promedio distorsiona cuando los folds tienen tamaños desiguales
        # (LOOCV, Spatial Block, Leave-P-Out, Bootstrap).
        M_final = np.sum(confusion_matrices, axis=0).astype(int)

        clases_lbl = [
            f"{c} - {id2cls.get(int(c), str(c))}" for c in global_clases_id]

        oa = accuracy_score(all_y_true, all_y_pred)
        # Kappa is better calculated on the sum
        kappa = float(_kappa(np.sum(confusion_matrices, axis=0)))
        f1_macro = f1_score(
            all_y_true,
            all_y_pred,
            average='macro',
            zero_division=0)
        f1_weighted = f1_score(
            all_y_true,
            all_y_pred,
            average='weighted',
            zero_division=0)
        f1_micro = f1_score(
            all_y_true,
            all_y_pred,
            average='micro',
            zero_division=0)

        metrics_dict = {
            'OA': oa, 'Accuracy': oa, 'F1_macro': f1_macro,
            'F1_weighted': f1_weighted, 'F1_micro': f1_micro, 'Kappa': kappa
        }

        # ── CSV 1: Matriz de confusión ──────────────────────────────────────
        ruta_csv = os.path.join(out_dir, "matriz_confusion.csv")
        with open(ruta_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([""] + clases_lbl)
            for i, lbl in enumerate(clases_lbl):
                w.writerow([lbl] + [int(v) for v in M_final[i]])

        # ── CSV 2: Métricas globales ─────────────────────────────────────────
        ruta_csv_global = os.path.join(out_dir, "metricas_globales.csv")
        with open(ruta_csv_global, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Metrica", "Valor"])
            w.writerow(["Overall Accuracy (OA)", f"{oa:.6f}"])
            w.writerow(["Kappa", f"{kappa:.6f}"])
            w.writerow(["F1 Macro", f"{f1_macro:.6f}"])
            w.writerow(["F1 Weighted", f"{f1_weighted:.6f}"])
            w.writerow(["F1 Micro", f"{f1_micro:.6f}"])
            w.writerow(["Algoritmo", method_name])
            w.writerow(["Validacion", val_method_name])
            w.writerow(["N Folds", str(len(cv_splits))])
            w.writerow(["Semilla", str(seed)])

        # classification_report primero — report_dict lo necesitan CSV 3,
        # heatmap y reporte HTML
        report_dict = classification_report(
            all_y_true, all_y_pred, output_dict=True, zero_division=0)
        report_text = classification_report(all_y_true, all_y_pred, zero_division=0,
                                            target_names=[id2cls.get(int(c), str(c)) for c in global_clases_id])

        img_cm_b64 = _plot_matriz_b64(M_final, clases_lbl, oa, kappa)
        img_heatmap_b64 = _plot_heatmap_b64(
            report_dict, clases_lbl, id2cls, global_clases_id)

        # ── CSV 3: Métricas por clase (precision, recall, especificidad, F1) ─
        ruta_csv_clase = os.path.join(out_dir, "metricas_por_clase.csv")
        with open(ruta_csv_clase, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Clase_ID", "Clase_Nombre", "Precision", "Recall_Sensibilidad",
                        "Especificidad", "F1_Score", "Soporte_px"])
            M_f = M_final.astype(np.float64)
            total_px = M_f.sum()
            for i, cls_id in enumerate(global_clases_id):
                cls_name = id2cls.get(int(cls_id), str(cls_id))
                cls_key = cls_name
                if cls_key not in report_dict:
                    cls_key = str(cls_id)
                if cls_key not in report_dict:
                    cls_key = str(int(cls_id))
                rd = report_dict.get(cls_key, {})
                prec = rd.get('precision', 0.0)
                rec = rd.get('recall', 0.0)
                f1c = rd.get('f1-score', 0.0)
                sup = int(rd.get('support', 0))
                tp = M_f[i, i]
                fn = M_f[i, :].sum() - tp
                fp = M_f[:, i].sum() - tp
                tn = total_px - tp - fn - fp
                spec = tn / (tn + fp) if (tn + fp) > 0 else 0.0
                w.writerow([cls_id, cls_name,
                            f"{prec:.6f}", f"{rec:.6f}",
                            f"{spec:.6f}", f"{f1c:.6f}", sup])

        # Entrenar modelo final para Feature Importance y prediccion raster
        feedback.pushInfo(
            "Entrenando modelo final y calculando Importancia de Variables...")
        final_model = model_factory(seed)
        final_model.fit(X_full, y_full)

        # Calcular Feature Importance
        importances = None
        if hasattr(final_model, 'feature_importances_'):
            importances = final_model.feature_importances_
        elif hasattr(final_model, 'model') and hasattr(final_model.model, 'feature_importances_'):
            importances = final_model.model.feature_importances_
        elif hasattr(final_model, 'coef_'):
            importances = np.abs(final_model.coef_[0])
        else:
            try:
                from sklearn.inspection import permutation_importance
                n_samples = min(2000, len(X_full))
                rng = np.random.RandomState(seed)
                idx = rng.choice(len(X_full), n_samples, replace=False)
                res = permutation_importance(final_model, X_full[idx], y_full[idx],
                                             n_repeats=3, random_state=seed, n_jobs=-1)
                importances = res.importances_mean
            except Exception:
                importances = np.ones(info["nbands"]) / info["nbands"]

        # ── CSV 4: Importancia de bandas ─────────────────────────────────────
        ruta_csv_imp = os.path.join(out_dir, "importancia_bandas.csv")
        imp_norm = np.maximum(importances, 0)
        if imp_norm.sum() > 0:
            imp_norm_pct = imp_norm / imp_norm.sum() * 100
        else:
            imp_norm_pct = imp_norm.copy()
        orden_imp = np.argsort(imp_norm_pct)[::-1]
        with open(ruta_csv_imp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Rango",
                        "Banda_Variable",
                        "Importancia_Raw",
                        "Importancia_Pct"])
            for rank, idx in enumerate(orden_imp, 1):
                w.writerow([rank, info["band_names"][idx],
                            f"{imp_norm[idx]:.6f}", f"{imp_norm_pct[idx]:.4f}"])

        img_imp_b64 = _plot_importance_b64(importances, info["band_names"])

        # Cargar logo institucional en base64
        logo_b64 = ""
        try:
            _logo_candidates = [
                os.path.join(os.path.dirname(__file__), "..",
                             "Icons", "logo_geomatica.png"),
                os.path.join(os.path.dirname(__file__), "..", "icon.png"),
            ]
            for _lp in _logo_candidates:
                if os.path.exists(_lp):
                    with open(_lp, "rb") as _lf:
                        logo_b64 = base64.b64encode(_lf.read()).decode("utf-8")
                    break
        except Exception as e:
            feedback.pushInfo(
                f"Aviso: no se pudo cargar el logo del reporte: {e}")

        # ── Reporte HTML completo ────────────────────────────────────────────
        desc_metodo = metodologia_text.get(val_method_name, val_method_name)
        ruta_html = _generate_html_report(
            out_dir=out_dir,
            method_name=method_name,
            val_method_name=val_method_name,
            metrics_dict=metrics_dict,
            M=M_final,
            clases_lbl=clases_lbl,
            report_text=report_text,
            img_cm_b64=img_cm_b64,
            img_heatmap_b64=img_heatmap_b64,
            img_imp_b64=img_imp_b64,
            metodologia_desc=desc_metodo,
            importances=importances,
            band_names=info["band_names"],
            id2cls=id2cls,
            global_clases_id=global_clases_id,
            report_dict=report_dict,
            raster_name=os.path.basename(raster_path),
            roi_name=vl.name(),
            n_pixeles=len(y_full),
            n_poligonos=vl.featureCount(),
            n_folds=len(cv_splits),
            seed=seed,
            logo_b64=logo_b64,
        )

        feedback.pushInfo(f"Overall Accuracy Global = {oa:.4f}")
        feedback.pushInfo(f"Kappa                  = {kappa:.4f}")
        feedback.pushInfo(f"F1 Macro               = {f1_macro:.4f}")
        feedback.pushInfo(f"Archivos CSV generados :")
        feedback.pushInfo(f"  - matriz_confusion.csv")
        feedback.pushInfo(f"  - metricas_globales.csv")
        feedback.pushInfo(f"  - metricas_por_clase.csv  (incl. especificidad)")
        feedback.pushInfo(f"  - importancia_bandas.csv")
        feedback.pushInfo(f"Reporte HTML Completo   : {ruta_html}")
        feedback.setProgress(70)

        # Predecir todo el raster por bloques
        feedback.pushInfo("Prediciendo el raster completo por bloques...")

        ds = gdal.Open(raster_path, gdal.GA_ReadOnly)
        ruta_clas = os.path.join(out_dir, "clasificacion.tif")
        drv = gdal.GetDriverByName("GTiff")
        w, h = info["cols"], info["rows"]
        ds_out = drv.Create(
            ruta_clas, w, h, 1, gdal.GDT_Int32,
            options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]
        )
        ds_out.SetGeoTransform(info["gt"])
        ds_out.SetProjection(info["proj"])
        bo = ds_out.GetRasterBand(1)
        bo.SetNoDataValue(0)
        bo.SetDescription("Clase ID")

        drv_mem = gdal.GetDriverByName("MEM")
        mask_ds = drv_mem.Create('', w, h, 1, gdal.GDT_Byte)
        mask_bo = mask_ds.GetRasterBand(1)

        chunk_size = 2048
        total_chunks = ((h + chunk_size - 1) // chunk_size) * \
            ((w + chunk_size - 1) // chunk_size)
        chunk_count = 0

        for yoff in range(0, h, chunk_size):
            if feedback.isCanceled():
                break
            ysize = min(chunk_size, h - yoff)
            for xoff in range(0, w, chunk_size):
                xsize = min(chunk_size, w - xoff)

                chunk_data = []
                for b in range(1, info["nbands"] + 1):
                    arr = ds.GetRasterBand(b).ReadAsArray(
                        xoff, yoff, xsize, ysize).astype(np.float32)
                    nd = info["nodata"][b - 1]
                    if nd is not None:
                        arr[arr == nd] = np.nan
                    chunk_data.append(arr)

                stack = np.stack(chunk_data, axis=-1)
                flat = stack.reshape(-1, info["nbands"])
                ok_all = np.isfinite(flat).all(axis=1)

                pred_all = np.zeros(flat.shape[0], dtype=np.int32)
                if ok_all.any():
                    pred_valid = final_model.predict(
                        flat[ok_all]).astype(np.int32)
                    pred_all[ok_all] = pred_valid

                out_arr = pred_all.reshape(ysize, xsize)
                bo.WriteArray(out_arr, xoff, yoff)
                mask_bo.WriteArray((out_arr > 0).astype(np.uint8), xoff, yoff)

                chunk_count += 1
                if chunk_count % max(1, total_chunks // 10) == 0:
                    feedback.setProgress(
                        70 + int(25 * chunk_count / total_chunks))

        ds_out.FlushCache()
        mask_ds.FlushCache()
        ds = None
        gc.collect()

        # Convertir a vector
        feedback.pushInfo("Generando vector clasificado...")
        ruta_vec = os.path.join(out_dir, "clasificacion_vector.shp")
        ds_vec = ogr.GetDriverByName(
            "ESRI Shapefile").CreateDataSource(ruta_vec)
        srs = osr.SpatialReference()
        if info["proj"]:
            srs.ImportFromWkt(info["proj"])

        layer_vec = ds_vec.CreateLayer("clasificacion", srs, ogr.wkbPolygon)
        layer_vec.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
        fd_name = ogr.FieldDefn("name", ogr.OFTString)
        fd_name.SetWidth(100)
        layer_vec.CreateField(fd_name)
        layer_vec.CreateField(ogr.FieldDefn("area_ha", ogr.OFTReal))

        gdal.Polygonize(bo, mask_bo, layer_vec, 0, [], callback=None)
        mask_ds = None

        for feat in layer_vec:
            c_id = feat.GetFieldAsInteger("ID")
            c_name = id2cls.get(c_id, str(c_id))
            feat.SetField("name", c_name)
            geom = feat.GetGeometryRef()
            if geom:
                feat.SetField("area_ha", geom.GetArea() / 10000.0)
            layer_vec.SetFeature(feat)

        ds_vec = None
        ds_out = None

        # Limpieza de temporales
        for tmp in (rast_id, rast_fid, tmp_roi):
            for ext in ("", ".dbf", ".shx", ".prj", ".cpg", ".qpj"):
                try:
                    p = tmp if ext == "" else tmp.replace(".shp", ext)
                    if os.path.exists(p):
                        os.remove(p)
                except Exception as e:
                    feedback.pushInfo(
                        f"Aviso: no se pudo borrar el temporal '{p}': {e}")

        feedback.pushInfo(f"Raster clasificado guardado en: {ruta_clas}")
        feedback.setProgress(100)
        feedback.pushInfo("Proceso COMPLETADO con exito.")

        return {self.OUT_FOLDER: out_dir}

    def run(self):
        processing.execAlgorithmDialog(self)
