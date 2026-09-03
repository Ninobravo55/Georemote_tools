import sys
import os
import tempfile
from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QPushButton, QLabel
from qgis.PyQt.QtCore import QProcess


class InstallDepsDialog(QDialog):
    def __init__(self, missing_deps, parent=None):
        super().__init__(parent)
        self.missing_deps = missing_deps
        self.setWindowTitle("Instalando Dependencias de GeomaticaPe")
        self.resize(650, 450)
        self.layout = QVBoxLayout(self)

        self.info_label = QLabel(
            f"Instalando paquetes requeridos: <b>{
                ', '.join(missing_deps)}</b><br>Por favor espera, no cierres esta ventana. El proceso puede tomar unos minutos dependiendo de la conexión a internet.")
        self.info_label.setWordWrap(True)
        self.layout.addWidget(self.info_label)

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        # Estilo tipo terminal (similar al progress dialog de dzetsaka)
        self.text_edit.setStyleSheet(
            "background-color: #1e1e1e; color: #00FF00; font-family: 'Consolas', 'Courier New', monospace; font-size: 12px; padding: 5px;")
        self.layout.addWidget(self.text_edit)

        self.close_btn = QPushButton("Cerrar (Instalación en progreso...)")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        self.layout.addWidget(self.close_btn)

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.handle_stdout)
        self.process.finished.connect(self.process_finished)

        self.start_installation()

    def start_installation(self):
        deps = ' '.join(self.missing_deps)
        osgeo4w_root = os.environ.get('OSGEO4W_ROOT')

        # Validación para PEP 668
        break_pkg = " --break-system-packages" if sys.version_info >= (
            3, 11) else ""

        # Obtener el directorio de python del perfil de QGIS para usar --target
        try:
            from qgis.core import QgsApplication
            target_dir = os.path.join(
                QgsApplication.qgisSettingsDirPath(), "python")
        except Exception:
            target_dir = ""

        target_arg = f'--target "{target_dir}"' if target_dir else '--user'

        self.text_edit.append("Iniciando instalación de dependencias...\n")
        if target_dir:
            self.text_edit.append(f"Directorio destino: {target_dir}\n")

        if os.name == 'nt' and osgeo4w_root and os.path.exists(
                os.path.join(osgeo4w_root, 'OSGeo4W.bat')):
            bat_path = os.path.join(osgeo4w_root, 'OSGeo4W.bat')
            # Usar OSGeo4W Shell para instalar limpiamente
            script_content = f"""@echo off
echo ========================================================
echo Instalando dependencias de Geomaticape Tools...
echo ========================================================
call "{bat_path}" python -m pip install {deps} {target_arg} --upgrade --disable-pip-version-check{break_pkg}
echo.
echo ========================================================
echo Ejecucion finalizada.
echo ========================================================
"""
            self.temp_bat = os.path.join(
                tempfile.gettempdir(),
                "install_geomaticape_deps.bat")
            with open(self.temp_bat, "w") as f:
                f.write(script_content)

            self.text_edit.append(f"$ cmd.exe /c {self.temp_bat}\n")
            self.process.start("cmd.exe", ["/c", self.temp_bat])
        else:
            # Fallback para MacOS/Linux o si no detecta OSGeo4W
            python_exe = os.path.join(
                sys.exec_prefix,
                'bin',
                'python3') if os.name != 'nt' else os.path.join(
                sys.exec_prefix,
                'python.exe')

            cmd_args = ["-m", "pip", "install", "--upgrade", *self.missing_deps]
            if target_dir:
                cmd_args.extend(["--target", target_dir])
            else:
                cmd_args.append("--user")
            cmd_args.append("--disable-pip-version-check")
            if sys.version_info >= (3, 11):
                cmd_args.append("--break-system-packages")

            cmd_str = f"{python_exe} {' '.join(cmd_args)}"
            self.text_edit.append(f"$ {cmd_str}\n")
            self.process.start(python_exe, cmd_args)

    def handle_stdout(self):
        data = self.process.readAllStandardOutput()
        stdout = bytes(data).decode("utf8", errors="replace")
        self.text_edit.insertPlainText(stdout)
        # Hacer auto-scroll hasta el final
        scrollbar = self.text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def process_finished(self, exitCode, exitStatus):
        self.close_btn.setText("Cerrar")
        self.close_btn.setEnabled(True)
        if exitCode == 0:
            self.info_label.setText(
                "<b>¡Instalación completada con éxito!</b> Por favor, <b>REINICIA QGIS</b> para aplicar los cambios.")
            self.info_label.setStyleSheet("color: #2E8B57; font-size: 13px;")
            self.text_edit.append("\n>>> INSTALACIÓN EXITOSA. Reinicie QGIS.")
        else:
            self.info_label.setText(
                "<b>La instalación terminó con errores.</b> Revisa la consola en color verde de abajo para más detalles.")
            self.info_label.setStyleSheet("color: #D32F2F; font-size: 13px;")
            self.text_edit.append(
                "\n>>> ERROR EN LA INSTALACIÓN. Por favor intente instalar manualmente.")

        # Intentar borrar el bat temporal
        try:
            if hasattr(self, 'temp_bat') and os.path.exists(self.temp_bat):
                os.remove(self.temp_bat)
        except BaseException:
            pass
