# app.py
import sqlite3
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import config
from recolector import open_db, recolectar

st.set_page_config(page_title="WoW Forever Market Analyzer", layout="wide")


def load_data() -> pd.DataFrame:
    conn = open_db()
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='price_snapshots'"
        ).fetchone()
        if not tables:
            return pd.DataFrame()
        df = pd.read_sql_query(
            """
            SELECT captured_at, ruleset_id, auction_house_id, item_id,
                   min_price, median_price, avg_price, total_quantity,
                   listings_count, origen
            FROM price_snapshots
            ORDER BY captured_at ASC
            """,
            conn,
        )
    finally:
        conn.close()

    if not df.empty:
        df["captured_dt"] = pd.to_datetime(df["captured_at"], utc=True)
        df["min_gold"] = df["min_price"] / 10000.0
        df["median_gold"] = df["median_price"] / 10000.0
        df["avg_gold"] = df["avg_price"] / 10000.0
    return df


def load_item_names() -> dict[int, str]:
    conn = open_db()
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='items_catalog'"
        ).fetchone()
        if not exists:
            return {}
        rows = conn.execute("SELECT item_id, name FROM items_catalog").fetchall()
        return {int(item_id): name for item_id, name in rows}
    finally:
        conn.close()


st.title("WoW Forever — Análisis de Mercado")
st.caption(
    "Base inicial de 3 días (`base_3d`) conservada en historial + actualizaciones bajo demanda (`manual`)."
)

st.sidebar.header("Actualización de datos")
auto_on_open = st.sidebar.checkbox("Actualizar al abrir la app", value=False)
btn_actualizar = st.sidebar.button("Actualizar mercado ahora (+500 nombres)", type="primary")

if "ran_on_open" not in st.session_state:
    st.session_state.ran_on_open = False

if btn_actualizar:
    with st.spinner("Consultando API de Blizzard y resolviendo hasta 500 nombres..."):
        res = recolectar()
    st.sidebar.success(
        f"Listo: +{res['inserted']} registros | +{res['names_added']} nombres "
        f"(quedan {res['names_remaining']})"
    )
elif auto_on_open and not st.session_state.ran_on_open:
    st.session_state.ran_on_open = True
    with st.spinner("Actualizando datos al iniciar la app..."):
        res = recolectar()
    st.sidebar.info(
        f"Al abrir: +{res['inserted']} registros | +{res['names_added']} nombres"
    )

df = load_data()
item_names = load_item_names()

if df.empty:
    st.warning("Todavía no hay datos en la base de datos. Pulsa 'Actualizar mercado ahora'.")
    st.stop()

pendientes_aprox = max(0, df["item_id"].nunique() - len(item_names))

col1, col2, col3, col4 = st.columns(4)
col1.metric("Capturas registradas", df["captured_at"].nunique())
col2.metric("Objetos únicos", df["item_id"].nunique())
col3.metric("Nombres resueltos", len(item_names))
col4.metric("Nombres por resolver", pendientes_aprox)

st.divider()

items_disponibles = sorted(df["item_id"].unique().tolist())
ah_disponibles = sorted(df["auction_house_id"].unique().tolist())


def format_item(iid: int) -> str:
    name = item_names.get(int(iid))
    return f"{name} ({iid})" if name else f"Item {iid}"


f_col1, f_col2, f_col3 = st.columns([2, 1, 1])
selected_item = f_col1.selectbox(
    "Selecciona objeto (escribe para buscar por nombre o ID)",
    options=items_disponibles,
    format_func=format_item,
)
selected_ah = f_col2.selectbox("Casa de subastas", options=["Todas"] + ah_disponibles)
metric_choice = f_col3.selectbox(
    "Precio principal",
    options=["median_gold", "min_gold", "avg_gold"],
    format_func=lambda x: {
        "median_gold": "Mediana (oro)",
        "min_gold": "Mínimo (oro)",
        "avg_gold": "Promedio (oro)",
    }[x],
)

sub = df[df["item_id"] == selected_item].copy()
if selected_ah != "Todas":
    sub = sub[sub["auction_house_id"] == selected_ah]

if sub.empty:
    st.info("No hay registros para este filtro.")
    st.stop()

agg_df = (
    sub.groupby(["captured_dt", "origen"], as_index=False)
    .agg(
        min_gold=("min_gold", "min"),
        median_gold=("median_gold", "median"),
        avg_gold=("avg_gold", "mean"),
        total_quantity=("total_quantity", "sum"),
        listings_count=("listings_count", "sum"),
    )
    .sort_values("captured_dt")
)

item_title = format_item(selected_item)

fig = make_subplots(
    rows=2,
    cols=1,
    shared_xaxes=True,
    vertical_spacing=0.1,
    row_heights=[0.68, 0.32],
    subplot_titles=(f"Evolución de precio — {item_title}", "Volumen / Stock disponible"),
)

fig.add_trace(
    go.Scatter(
        x=agg_df["captured_dt"],
        y=agg_df[metric_choice],
        mode="lines",
        name="Tendencia de precio",
    ),
    row=1,
    col=1,
)

for origen_val, label_txt, symbol_shape in [
    ("base_3d", "Base 3 días", "circle"),
    ("manual", "Petición / actualización", "diamond"),
]:
    part = agg_df[agg_df["origen"] == origen_val]
    if not part.empty:
        fig.add_trace(
            go.Scatter(
                x=part["captured_dt"],
                y=part[metric_choice],
                mode="markers",
                name=label_txt,
                marker=dict(size=9, symbol=symbol_shape),
            ),
            row=1,
            col=1,
        )

fig.add_trace(
    go.Bar(
        x=agg_df["captured_dt"],
        y=agg_df["total_quantity"],
        name="Cantidad total",
    ),
    row=2,
    col=1,
)

fig.update_layout(height=620, hovermode="x unified")
fig.update_yaxes(title_text="Oro", row=1, col=1)
fig.update_yaxes(title_text="Unidades", row=2, col=1)

st.plotly_chart(fig, use_container_width=True)

with st.expander("Ver tabla de registros del producto"):
    st.dataframe(
        agg_df.sort_values("captured_dt", ascending=False),
        use_container_width=True,
    )