"""Look and feel: page config, CSS, and the colors charts share."""
from __future__ import annotations

import streamlit as st

INK = "#20291f"
LEAF = "#2f6b4f"
PAPER = "#f5f6ef"
UP = "#1f8a55"          # gains / positive news
DOWN = "#d14b43"        # losses / negative news
NEUTRAL = "#9aa39b"
HEAT = "#2878b5"        # search interest
ACCENT = "#e28a32"      # MA20, highlights
SENTIMENT_COLORS = {"Positive": UP, "Neutral": NEUTRAL, "Negative": DOWN, "Unanalyzed": "#cfd5cc"}
SENTIMENT_NAMES = {"Positive": "正面", "Neutral": "中性", "Negative": "負面", "Unanalyzed": "未分析"}


def configure_app() -> None:
    st.set_page_config(page_title="Trends × Theme 熱搜與股價", page_icon="📈", layout="wide")
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Noto+Sans+TC:wght@400;500;600;700&display=swap');
        :root {{ --ink: {INK}; --leaf: {LEAF}; --paper: {PAPER}; }}
        html, body, [class*="css"] {{ font-family: 'DM Sans', 'Noto Sans TC', sans-serif; color: var(--ink); }}
        .stApp {{ background: radial-gradient(ellipse at 92% 0%, #e5edd4 0, transparent 35%), var(--paper); }}
        h1, h2, h3 {{ color: var(--ink); letter-spacing: 0; }}
        [data-testid="stSidebar"] {{ background: {INK}; }}
        [data-testid="stSidebar"] * {{ color: {PAPER}; }}
        div[data-testid="stDataFrame"] {{ border: 1px solid #e1e6da; }}
        .lead {{ font-size: 1.08rem; line-height: 1.75; color: #3b463a; }}
        .chip {{ display: inline-block; padding: 1px 9px; margin: 0 4px 4px 0; border-radius: 999px;
                font-size: .82rem; font-weight: 600; background: #e6ebe0; color: {INK}; }}
        .chip.up {{ background: #dcefe3; color: {UP}; }}
        .chip.down {{ background: #f6dedc; color: {DOWN}; }}
        .chip.heat {{ background: #dceaf5; color: {HEAT}; }}
        .chip.flat {{ background: #eceee9; color: #5d665c; }}
        .badge {{ display: inline-flex; align-items: center; justify-content: center; width: 1.7rem; height: 1.7rem;
                 border-radius: 50%; background: {INK}; color: {PAPER}; font-weight: 700; margin-right: .5rem; }}
        .step {{ font-size: 2rem; font-weight: 700; color: {LEAF}; line-height: 1; }}
        .muted {{ color: #6b7569; font-size: .88rem; }}
        </style>
        """,
        unsafe_allow_html=True,
    )
