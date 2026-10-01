import streamlit as st
import pandas as pd
import smtplib
import io
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature
from datetime import datetime

# ============================================================
# CONFIGURACIÓN DE PÁGINA
# ============================================================
st.set_page_config(
    page_title="RandPunkt — Gestión de Aprobaciones",
    page_icon="randpunkt-favicon.png",
    layout="wide"
)

# ============================================================
# COLUMNAS DE LOS CSV
# ============================================================
COLS_ARTICULOS = ["articulo_id", "titulo", "ruta_pdf", "fecha_envio", "estado"]
COLS_CONFIRMACIONES = [
    "articulo_id", "nombre", "email", "token",
    "aprobado", "fecha_aprobacion", "comentarios"
]

# ============================================================
# CAPA SFTP — TODA LA PERSISTENCIA VA AL SERVIDOR REMOTO
# ============================================================
def _sftp_conectar():
    """Abre una conexión SFTP al servidor remoto."""
    import paramiko
    rs = st.secrets["remote_server"]
    transport = paramiko.Transport((rs["host"], int(rs["port"])))
    transport.connect(username=rs["user"], password=rs["password"])
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp

def _ruta_remota(nombre_archivo: str) -> str:
    rs = st.secrets["remote_server"]
    return f"{rs['dir'].rstrip('/')}/{nombre_archivo}"

def _asegurar_directorio(sftp, ruta_dir):
    """Crea un directorio remoto si no existe."""
    try:
        sftp.stat(ruta_dir)
    except IOError:
        sftp.mkdir(ruta_dir)

def leer_csv_remoto(nombre_archivo: str, columnas: list) -> pd.DataFrame:
    """Lee un CSV desde el servidor remoto. Si no existe, devuelve vacío."""
    try:
        transport, sftp = _sftp_conectar()
        ruta = _ruta_remota(nombre_archivo)
        try:
            with sftp.open(ruta, "r") as f:
                contenido = f.read().decode("utf-8")
            df = pd.read_csv(io.StringIO(contenido), dtype=str).fillna("")
            for c in columnas:
                if c not in df.columns:
                    df[c] = ""
            df = df[columnas]
        except FileNotFoundError:
            df = pd.DataFrame(columns=columnas)
        sftp.close()
        transport.close()
        return df
    except Exception as e:
        st.error(f"Error al leer {nombre_archivo} del servidor remoto: {e}")
        return pd.DataFrame(columns=columnas)

def guardar_csv_remoto(nombre_archivo: str, df: pd.DataFrame) -> bool:
    """Escribe un CSV en el servidor remoto."""
    try:
        transport, sftp = _sftp_conectar()
        ruta = _ruta_remota(nombre_archivo)
        contenido = df.to_csv(index=False).encode("utf-8")
        with sftp.open(ruta, "w") as f:
            f.write(contenido)
        sftp.close()
        transport.close()
        return True
    except Exception as e:
        st.error(f"Error al guardar {nombre_archivo} en el servidor remoto: {e}")
        return False

def leer_articulos() -> pd.DataFrame:
    return leer_csv_remoto(st.secrets["csv"]["articulos_file"], COLS_ARTICULOS)

def leer_confirmaciones() -> pd.DataFrame:
    return leer_csv_remoto(st.secrets["csv"]["confirmaciones_file"], COLS_CONFIRMACIONES)

def guardar_articulos(df: pd.DataFrame) -> bool:
    return guardar_csv_remoto(st.secrets["csv"]["articulos_file"], df)

def guardar_confirmaciones(df: pd.DataFrame) -> bool:
    return guardar_csv_remoto(st.secrets["csv"]["confirmaciones_file"], df)

def siguiente_id_articulo() -> str:
    df = leer_articulos()
    if df.empty:
        return "ART-0001"
    try:
        max_num = max(int(x.split("-")[1]) for x in df["articulo_id"])
        return f"ART-{max_num + 1:04d}"
    except Exception:
        return "ART-0001"

def subir_pdf_sftp(archivo_bytes: bytes, nombre_remoto: str):
    """Sube un PDF al servidor remoto en la carpeta manuscritos/."""
    try:
        import paramiko
        rs = st.secrets["remote_server"]
        transport = paramiko.Transport((rs["host"], int(rs["port"])))
        transport.connect(username=rs["user"], password=rs["password"])
        sftp = paramiko.SFTPClient.from_transport(transport)

        dir_manuscritos = f"{rs['dir'].rstrip('/')}/manuscritos"
        _asegurar_directorio(sftp, dir_manuscritos)

        ruta_remota = f"{dir_manuscritos}/{nombre_remoto}"
        with sftp.open(ruta_remota, "wb") as f:
            f.write(archivo_bytes)

        sftp.close()
        transport.close()

        url_publica = f"{rs['public_url'].rstrip('/')}/manuscritos/{nombre_remoto}"
        return True, url_publica
    except Exception as e:
        return False, str(e)

# ============================================================
# TOKENS
# ============================================================
def get_serializer():
    return URLSafeTimedSerializer(st.secrets["app"]["secret_key"])

def generar_token(email: str, articulo_id: str, nombre: str) -> str:
    s = get_serializer()
    return s.dumps(
        {"email": email, "articulo_id": articulo_id, "nombre": nombre},
        salt="aprobacion-autor"
    )

def validar_token(token: str):
    s = get_serializer()
    try:
        data = s.loads(token, salt="aprobacion-autor", max_age=2592000)  # 30 días
        return data, None
    except SignatureExpired:
        return None, "El enlace ha expirado (más de 30 días)."
    except BadTimeSignature:
        return None, "El enlace no es válido o fue manipulado."

# ============================================================
# CORREO HTML CON LOGO
# ============================================================
def construir_html_correo(nombre, titulo, enlace, art_id, es_recordatorio=False):
    cfg = st.secrets["app"]
    email_cfg = st.secrets["email"]

    logo_url = cfg["logo_url"]
    journal = cfg["journal_name"]
    series = cfg["journal_series"]
    issn = cfg["journal_issn"]
    editor_nombre = email_cfg["sender_name"]
    editor_email = email_cfg["notification_email"]

    prefijo = "Recordatorio: " if es_recordatorio else ""
    intro = (
        "Le recordamos que aún no hemos recibido su confirmación de "
        "aprobación para el siguiente manuscrito. Por favor, revise "
        "el enlace a continuación."
        if es_recordatorio
        else
        "Como parte del proceso editorial, le solicitamos revisar y "
        "aprobar el manuscrito antes de su publicación."
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>{prefijo}Aprobación de manuscrito</title>
</head>
<body style="margin:0; padding:0; background:#F5F4F0;
             font-family: Georgia, 'Times New Roman', serif;
             color:#1B2A4A; line-height:1.6;">
  <div style="max-width:620px; margin:0 auto; padding:24px 12px;">

    <div style="background:#FFFFFF; padding:24px 28px;
                border-bottom:3px solid #5B7A99; text-align:center;
                border-radius:2px 2px 0 0;">
      <img src="{logo_url}" alt="{journal}"
           style="max-width:240px; height:auto; display:block;
                  margin:0 auto 10px auto;">
      <p style="margin:0; font-size:12px; letter-spacing:0.05em;
                color:#8A8F98; text-transform:uppercase;">
        {series} &nbsp;&bull;&nbsp; ISSN {issn}
      </p>
    </div>

    <div style="background:#FFFFFF; padding:32px 28px;">
      <p style="margin-top:0;">Estimado/a <strong>{nombre}</strong>,</p>

      <p>{intro}</p>

      <p style="margin:24px 0 8px 0;">Manuscrito:</p>
      <p style="margin:0 0 24px 0; padding:12px 16px;
                background:#F5F4F0; border-left:3px solid #B08D57;
                font-style:italic;">
        &ldquo;{titulo}&rdquo;<br>
        <span style="font-size:12px; color:#8A8F98;
                     font-style:normal;">Referencia: {art_id}</span>
      </p>

      <p>Para confirmar que ha leído el manuscrito y está de acuerdo
         con su contenido, haga clic en el siguiente botón:</p>

      <p style="text-align:center; margin:32px 0;">
        <a href="{enlace}"
           style="display:inline-block; background:#5B7A99; color:#FFFFFF;
                  padding:13px 32px; text-decoration:none;
                  border-radius:2px; font-family: Arial, sans-serif;
                  font-weight:bold; font-size:15px;
                  letter-spacing:0.03em;">
          Confirmar aprobación
        </a>
      </p>

      <p style="font-size:13px; color:#8A8F98; margin-top:24px;">
        El enlace es personal e intransferible y expira en 30 días.
        Si tiene comentarios o sugerencias, puede incluirlos al
        momento de confirmar su aprobación.
      </p>

      <p style="margin-top:28px;">Saludos cordiales,<br>
         <strong>{editor_nombre}</strong><br>
         <span style="color:#8A8F98; font-size:13px;">{journal}</span>
      </p>
    </div>

    <div style="background:#F5F4F0; padding:16px 28px; font-size:11px;
                color:#8A8F98; text-align:center;
                border-top:1px solid #E8E6E1;
                border-radius:0 0 2px 2px;">
      Este correo fue enviado automáticamente por el sistema editorial
      de {journal}.<br>
      Si lo recibió por error, notifíquelo a
      <a href="mailto:{editor_email}" style="color:#5B7A99;
         text-decoration:none;">{editor_email}</a>.
    </div>

  </div>
</body>
</html>"""


def construir_texto_plano(nombre, titulo, enlace, art_id, es_recordatorio=False):
    cfg = st.secrets["app"]
    email_cfg = st.secrets["email"]
    journal = cfg["journal_name"]
    editor_email = email_cfg["notification_email"]
    editor_nombre = email_cfg["sender_name"]

    prefijo = "RECORDATORIO: " if es_recordatorio else ""
    intro = (
        "Le recordamos que aún no hemos recibido su confirmación de "
        "aprobación para el siguiente manuscrito."
        if es_recordatorio
        else
        "Como parte del proceso editorial, le solicitamos revisar y "
        "aprobar el manuscrito antes de su publicación."
    )

    return f"""{prefijo}Aprobación de manuscrito — {journal}

Estimado/a {nombre},

{intro}

Manuscrito: "{titulo}"
Referencia: {art_id}

Para confirmar que ha leído el manuscrito y está de acuerdo con su
contenido, visite el siguiente enlace:

{enlace}

El enlace es personal e intransferible y expira en 30 días.
Si tiene comentarios o sugerencias, puede incluirlos al momento de
confirmar su aprobación.

Saludos cordiales,
{editor_nombre}
{journal}

---
Este correo fue enviado automáticamente por el sistema editorial de
{journal}. Si lo recibió por error, notifíquelo a {editor_email}.
"""


def enviar_correo(destinatario, nombre, enlace, titulo, art_id,
                  es_recordatorio=False) -> bool:
    try:
        cfg = st.secrets["email"]
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg['sender_name']} <{cfg['sender_email']}>"
        msg["To"] = destinatario
        prefijo = "Recordatorio — " if es_recordatorio else ""
        msg["Subject"] = f"{prefijo}[RandPunkt] Aprobación requerida: {titulo}"

        texto = construir_texto_plano(nombre, titulo, enlace, art_id, es_recordatorio)
        html = construir_html_correo(nombre, titulo, enlace, art_id, es_recordatorio)

        msg.attach(MIMEText(texto, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        server = smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]))
        server.starttls()
        server.login(cfg["sender_email"], cfg["sender_password"])
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        st.error(f"Error al enviar correo a {destinatario}: {e}")
        return False


def enviar_notificacion_editor(mensaje: str):
    try:
        cfg = st.secrets["email"]
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg['sender_name']} <{cfg['sender_email']}>"
        msg["To"] = cfg["notification_email"]
        msg["Subject"] = "[RandPunkt] Todos los autores han aprobado un manuscrito"
        msg.attach(MIMEText(mensaje, "plain", "utf-8"))

        server = smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]))
        server.starttls()
        server.login(cfg["sender_email"], cfg["sender_password"])
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        st.warning(f"No se pudo notificar al editor: {e}")
        return False

# ============================================================
# VISTA DEL EDITOR
# ============================================================
def autenticar_editor() -> bool:
    if "editor_autenticado" not in st.session_state:
        st.session_state.editor_autenticado = False
    if st.session_state.editor_autenticado:
        return True

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.title("🔒 Panel del Editor")
        st.caption("RandPunkt Journal — Sistema de aprobaciones")
        with st.form("login_editor"):
            clave = st.text_input("Contraseña de editor", type="password")
            if st.form_submit_button("Entrar", use_container_width=True):
                if clave == st.secrets["app"]["editor_password"]:
                    st.session_state.editor_autenticado = True
                    st.rerun()
                else:
                    st.error("Contraseña incorrecta.")
    return False


def vista_editor():
    with st.sidebar:
        try:
            st.image("randpunkt-logo.png", use_container_width=True)
        except Exception:
            st.markdown("### RandPunkt Journal")
        st.caption("Panel editorial")
        st.divider()
        st.write(f"**Editor:** {st.secrets['email']['sender_name']}")
        st.write(f"**Notificaciones:** {st.secrets['email']['notification_email']}")
        st.divider()
        if st.button("🔒 Cerrar sesión", use_container_width=True):
            st.session_state.editor_autenticado = False
            st.rerun()

    st.title("📚 Gestión de Aprobaciones Editoriales")

    tab1, tab2, tab3 = st.tabs([
        "📤 Nuevo envío",
        "📋 Estado por artículo",
        "📊 Registro completo"
    ])

    # ---------- TAB 1: NUEVO ENVÍO ----------
    with tab1:
        st.subheader("Registrar manuscrito y solicitar aprobaciones")

        with st.form("form_nuevo", clear_on_submit=False):
            titulo = st.text_input("Título del manuscrito")
            url_pdf = st.text_input(
                "URL pública del PDF (o súbalo abajo vía SFTP)"
            )
            autores_raw = st.text_area(
                "Autores (uno por línea, formato: Nombre <email>)",
                height=180,
                placeholder="Juan Pérez <juan@ejemplo.com>\nMaría Gómez <maria@ejemplo.com>"
            )
            enviar = st.form_submit_button(
                "📨 Enviar solicitudes de aprobación",
                use_container_width=True
            )

        st.divider()
        st.subheader("📎 Subir PDF al servidor remoto (opcional)")
        with st.form("form_pdf"):
            archivo = st.file_uploader(
                "Seleccione el PDF del manuscrito", type=["pdf"]
            )
            subir = st.form_submit_button(
                "⬆️ Subir PDF vía SFTP", use_container_width=True
            )

        if subir and archivo is not None:
            with st.spinner("Subiendo PDF al servidor remoto..."):
                nombre_remoto = (
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{archivo.name}"
                )
                ok, resultado = subir_pdf_sftp(archivo.getbuffer(), nombre_remoto)
                if ok:
                    st.success(f"PDF subido: {resultado}")
                    st.info("Copie esta URL en el campo 'URL pública del PDF'.")
                else:
                    st.error(f"Error al subir: {resultado}")

        if enviar:
            if not titulo or not autores_raw.strip():
                st.error("El título y al menos un autor son obligatorios.")
            else:
                autores, errores = [], []
                for linea in autores_raw.strip().split("\n"):
                    linea = linea.strip()
                    if not linea:
                        continue
                    if "<" in linea and ">" in linea:
                        nombre = linea.split("<")[0].strip()
                        email = linea.split("<")[1].split(">")[0].strip()
                        if nombre and email:
                            autores.append((nombre, email))
                        else:
                            errores.append(linea)
                    else:
                        errores.append(linea)

                if errores:
                    st.warning(f"Líneas ignoradas: {errores}")
                if not autores:
                    st.error("No se detectaron autores válidos.")
                else:
                    art_id = siguiente_id_articulo()
                    df_art = leer_articulos()
                    nueva_fila = {
                        "articulo_id": art_id,
                        "titulo": titulo,
                        "ruta_pdf": url_pdf,
                        "fecha_envio": datetime.now().isoformat(timespec="seconds"),
                        "estado": "pendiente_aprobacion"
                    }
                    df_art = pd.concat(
                        [df_art, pd.DataFrame([nueva_fila])],
                        ignore_index=True
                    )
                    guardar_articulos(df_art)

                    df_conf = leer_confirmaciones()
                    base_url = st.secrets["app"]["base_url"].rstrip("/")
                    exitos = 0
                    for nombre, email in autores:
                        token = generar_token(email, art_id, nombre)
                        enlace = f"{base_url}/?token={token}"
                        if enviar_correo(email, nombre, enlace, titulo, art_id):
                            fila = {
                                "articulo_id": art_id,
                                "nombre": nombre,
                                "email": email,
                                "token": token,
                                "aprobado": "0",
                                "fecha_aprobacion": "",
                                "comentarios": ""
                            }
                            df_conf = pd.concat(
                                [df_conf, pd.DataFrame([fila])],
                                ignore_index=True
                            )
                            exitos += 1

                    guardar_confirmaciones(df_conf)
                    st.success(
                        f"✅ Artículo {art_id} registrado. "
                        f"Se enviaron {exitos}/{len(autores)} correos."
                    )

    # ---------- TAB 2: ESTADO POR ARTÍCULO ----------
    with tab2:
        st.subheader("Estado de aprobaciones")
        df_art = leer_articulos()
        df_conf = leer_confirmaciones()

        if df_art.empty:
            st.info("Aún no hay artículos registrados.")
        else:
            for _, art in df_art.iterrows():
                autores_art = df_conf[df_conf["articulo_id"] == art["articulo_id"]]
                total = len(autores_art)
                aprobados = (autores_art["aprobado"] == "1").sum() if total else 0

                icono = "✅" if total > 0 and aprobados == total else "⏳"
                with st.expander(
                    f"{icono} {art['articulo_id']} — {art['titulo']}  "
                    f"({aprobados}/{total} aprobados)"
                ):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.write(f"**Estado:** `{art['estado']}`")
                        st.write(f"**Fecha de envío:** {art['fecha_envio']}")
                    with col2:
                        if art["ruta_pdf"]:
                            st.markdown(f"**PDF:** [Ver]({art['ruta_pdf']})")
                        else:
                            st.write("**PDF:** —")

                    if total == 0:
                        st.info("No hay autores registrados.")
                    else:
                        tabla = autores_art[[
                            "nombre", "email", "aprobado",
                            "fecha_aprobacion", "comentarios"
                        ]].copy()
                        tabla["aprobado"] = tabla["aprobado"].map(
                            {"1": "✅ Sí", "0": "⏳ Pendiente"}
                        )
                        st.dataframe(tabla, use_container_width=True)

                        pendientes = autores_art[autores_art["aprobado"] == "0"]
                        if len(pendientes) > 0:
                            if st.button(
                                f"📧 Enviar recordatorio a "
                                f"{len(pendientes)} pendiente(s)",
                                key=f"rec_{art['articulo_id']}"
                            ):
                                base_url = st.secrets["app"]["base_url"].rstrip("/")
                                enviados = 0
                                for _, p in pendientes.iterrows():
                                    enlace = f"{base_url}/?token={p['token']}"
                                    if enviar_correo(
                                        p["email"], p["nombre"], enlace,
                                        art["titulo"], art["articulo_id"],
                                        es_recordatorio=True
                                    ):
                                        enviados += 1
                                st.success(f"Recordatorios enviados: {enviados}")

                    if total > 0 and aprobados == total and art["estado"] != "publicado":
                        if st.button(
                            f"🚀 Marcar {art['articulo_id']} como publicado",
                            key=f"pub_{art['articulo_id']}"
                        ):
                            df_art.loc[
                                df_art["articulo_id"] == art["articulo_id"],
                                "estado"
                            ] = "publicado"
                            guardar_articulos(df_art)
                            st.success(f"Artículo {art['articulo_id']} publicado.")
                            st.rerun()

    # ---------- TAB 3: REGISTRO COMPLETO ----------
    with tab3:
        st.subheader("Registro completo de confirmaciones")
        df_conf = leer_confirmaciones()
        if df_conf.empty:
            st.info("Aún no hay confirmaciones registradas.")
        else:
            df_mostrar = df_conf.drop(columns=["token"])
            st.dataframe(df_mostrar, use_container_width=True)
            csv_data = df_conf.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ Descargar CSV de confirmaciones",
                data=csv_data,
                file_name=f"confirmaciones_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv"
            )

# ============================================================
# VISTA DEL AUTOR (pública)
# ============================================================
def vista_autor(token: str):
    st.title("✍️ Aprobación de Manuscrito")
    data, error = validar_token(token)
    if error:
        st.error(error)
        return

    email = data["email"]
    art_id = data["articulo_id"]
    nombre = data["nombre"]

    df_art = leer_articulos()
    df_conf = leer_confirmaciones()

    art_row = df_art[df_art["articulo_id"] == art_id]
    if art_row.empty:
        st.error("El artículo asociado ya no existe.")
        return
    art = art_row.iloc[0]

    st.write(
        f"Estimado/a **{nombre}**, ha sido invitado/a a aprobar "
        f"el siguiente manuscrito:"
    )
    st.markdown(f"### {art['titulo']}")
    st.caption(f"Referencia editorial: {art_id}")

    if art["ruta_pdf"]:
        st.markdown(f"📄 [Ver manuscrito PDF]({art['ruta_pdf']})")

    fila = df_conf[(df_conf["articulo_id"] == art_id) &
                   (df_conf["email"] == email)]
    if fila.empty:
        st.error("No se encontró su registro de autor para este artículo.")
        return

    ya_aprobado = fila.iloc[0]["aprobado"] == "1"

    if ya_aprobado:
        st.success(
            f"✅ Ya ha aprobado este manuscrito el "
            f"{fila.iloc[0]['fecha_aprobacion']}."
        )
        st.info("Gracias por su confirmación.")
        return

    with st.form("form_aprobacion"):
        st.write("Por favor, confirme lo siguiente:")
        acepto = st.checkbox(
            "He leído el manuscrito en su totalidad y estoy de acuerdo "
            "con su contenido para publicación."
        )
        comentarios = st.text_area(
            "Comentarios opcionales para el editor", height=120
        )
        enviar = st.form_submit_button(
            "✅ Confirmar aprobación", use_container_width=True
        )

    if enviar:
        if not acepto:
            st.warning("Debe marcar la casilla para confirmar su aprobación.")
            return

        idx = df_conf.index[
            (df_conf["articulo_id"] == art_id) & (df_conf["email"] == email)
        ]
        df_conf.loc[idx, "aprobado"] = "1"
        df_conf.loc[idx, "fecha_aprobacion"] = datetime.now().isoformat(
            timespec="seconds"
        )
        df_conf.loc[idx, "comentarios"] = comentarios
        guardar_confirmaciones(df_conf)

        st.success("¡Gracias! Su aprobación ha sido registrada.")
        st.balloons()

        autores_art = df_conf[df_conf["articulo_id"] == art_id]
        if (autores_art["aprobado"] == "1").all():
            df_art.loc[df_art["articulo_id"] == art_id, "estado"] = "todos_aprobados"
            guardar_articulos(df_art)
            enviar_notificacion_editor(
                f"Todos los autores del artículo {art_id} "
                f"('{art['titulo']}') han aprobado el manuscrito. "
                f"Puede proceder a publicarlo."
            )

# ============================================================
# ENRUTADOR PRINCIPAL
# ============================================================
def main():
    query_params = st.query_params
    token = query_params.get("token")

    if token:
        vista_autor(token)
    else:
        if autenticar_editor():
            vista_editor()

if __name__ == "__main__":
    main()
