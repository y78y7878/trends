"""Plotly figures. Every chart says in its own title and axis labels what it shows."""
from __future__ import annotations

import html
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from trends.web.style import ACCENT, DOWN, HEAT, INK, NEUTRAL, SENTIMENT_COLORS, SENTIMENT_NAMES, UP

HEAT_PALETTE = ("#2878b5", "#7b5ea7", "#2a9d8f", "#c06c84", "#8d6e3f")
WINDOW_DAYS = 10


def _pct(value: object) -> str:
    return "—" if value is None or pd.isna(value) else f"{float(value):+.1%}"


def _event_hover(event: pd.Series) -> str:
    lines = [
        f"<b>事件 {event['number']}｜{event['event_date']:%Y-%m-%d}</b>",
        "關鍵字：" + "、".join(event["keywords"][:4]),
        "原因：" + "、".join(event["triggers"]),
    ]
    if event["news_count"]:
        lines.append(f"新聞 {event['news_count']} 則（正面 {event['positive']}、負面 {event['negative']}）")
        for item in event["headlines"][:3]:
            title = html.escape(str(item["news_title"]))
            lines.append("・" + (title[:42] + "…" if len(title) > 42 else title))
    lines.append(f"之後 10 日：{_pct(event.get('return_10d'))}（扣大盤 {_pct(event.get('excess_10d'))}）")
    return "<br>".join(lines)


def stock_event_chart(data, start: pd.Timestamp, end: pd.Timestamp, show_ma: bool = True) -> go.Figure:
    """Three stacked panels on one time axis: price with numbered event flags and the 10 trading
    days after each event shaded by outcome; search interest per keyword; daily news by tone."""
    prices = data.prices[data.prices["date"].between(start, end)]
    events = data.events[data.events["event_date"].between(start, end)] if not data.events.empty else data.events
    heat = data.heat[data.heat["date"].between(start, end)]
    news = data.news_daily[data.news_daily["date"].between(start, end)]

    figure = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.58, 0.22, 0.20],
        subplot_titles=("① 股價：圓圈數字 = 事件；圓圈顏色與加粗線段 = 事件後 10 個交易日扣掉大盤的表現（綠贏、紅輸、灰尚無資料）",
                        "② 搜尋熱度：關鍵字在 Google 的熱門程度（0–100）",
                        "③ 每日相關新聞數（依 AI 判斷的情緒上色）"),
    )
    figure.add_trace(go.Scatter(x=prices["date"], y=prices["close"], name="收盤價", mode="lines",
                                line={"color": INK, "width": 2.2}, hovertemplate="%{x|%Y-%m-%d}<br>收盤 %{y:,.2f}<extra></extra>"),
                     row=1, col=1)
    if show_ma:
        figure.add_trace(go.Scatter(x=prices["date"], y=prices["ma20"], name="20 日均線", mode="lines",
                                    line={"color": ACCENT, "width": 1.3, "dash": "dot"}, hoverinfo="skip"), row=1, col=1)

    # Each event's next 10 trading days are re-drawn as a thick translucent segment of the price
    # line in the outcome's color; overlapping windows overlay instead of darkening a background.
    trading_days = list(data.prices["date"])
    closes = data.prices.set_index("date")["close"]
    colors = []
    for _, event in events.iterrows():
        outcome = event.get("excess_10d")
        if outcome is None or pd.isna(outcome):
            outcome = event.get("excess_5d")
        color = NEUTRAL if outcome is None or pd.isna(outcome) else (UP if outcome > 0 else DOWN)
        colors.append(color)
        day0 = pd.Timestamp(event["market_date"])
        if day0 not in trading_days:
            continue
        index = trading_days.index(day0)
        window = data.prices.iloc[index:index + WINDOW_DAYS + 1]
        figure.add_trace(go.Scatter(x=window["date"], y=window["close"], mode="lines", showlegend=False, hoverinfo="skip",
                                    line={"color": color, "width": 9}, opacity=0.28), row=1, col=1)
        figure.add_vline(x=day0, line={"color": "#7d877b", "width": 1, "dash": "dot"}, opacity=0.35)

    if not events.empty:
        flags = events.assign(y=events["market_date"].map(lambda d: closes.get(pd.Timestamp(d))))
        figure.add_trace(go.Scatter(
            x=flags["market_date"], y=flags["y"], mode="markers+text", name="事件（顏色＝之後 10 日扣大盤）",
            text=flags["number"].astype(str), textfont={"color": "#ffffff", "size": 11},
            marker={"size": 22, "color": colors, "line": {"color": "#ffffff", "width": 2}},
            hovertext=[_event_hover(e) for _, e in flags.iterrows()], hoverinfo="text",
        ), row=1, col=1)

    top_keywords = (heat.groupby("keyword")["heat"].max().sort_values(ascending=False).head(len(HEAT_PALETTE)).index)
    for color, keyword in zip(HEAT_PALETTE, top_keywords):
        series = heat[heat["keyword"].eq(keyword)].sort_values("date")
        figure.add_trace(go.Scatter(x=series["date"], y=series["heat"], name=f"熱度：{keyword}", mode="lines",
                                    line={"color": color, "width": 1.8},
                                    hovertemplate=f"{html.escape(keyword)}<br>%{{x|%Y-%m-%d}}　熱度 %{{y:.0f}}<extra></extra>"),
                         row=2, col=1)

    for sentiment in ("Positive", "Neutral", "Negative", "Unanalyzed"):
        if sentiment in news and news[sentiment].sum():
            figure.add_trace(go.Bar(x=news["date"], y=news[sentiment], name=f"新聞：{SENTIMENT_NAMES[sentiment]}",
                                    marker_color=SENTIMENT_COLORS[sentiment],
                                    hovertemplate=f"%{{x|%Y-%m-%d}}　{SENTIMENT_NAMES[sentiment]} %{{y}} 則<extra></extra>"),
                             row=3, col=1)

    figure.update_layout(
        height=760, barmode="stack", template="plotly_white", hovermode="closest",
        legend={"orientation": "h", "y": -0.08, "x": 0}, margin={"t": 40, "b": 10, "l": 10, "r": 10},
        hoverlabel={"align": "left"},
    )
    figure.update_annotations(font={"size": 13}, x=0, xanchor="left")
    figure.update_yaxes(title_text="股價", row=1, col=1)
    figure.update_yaxes(range=[0, 105], row=2, col=1)
    figure.update_xaxes(range=[start, end + pd.Timedelta(days=1)])
    return figure


def car_chart(paths: pd.DataFrame) -> go.Figure:
    """Average cumulative abnormal return around the trend day, with a 95% band."""
    names = {"direct": "公司本身上熱搜（直接事件）", "theme": "產業詞上熱搜 → 同類股（主題事件）"}
    colors = {"direct": INK, "theme": HEAT}
    figure = go.Figure()
    for kind in ("direct", "theme"):
        path = paths[paths["kind"].eq(kind)].sort_values("offset")
        if path.empty:
            continue
        upper, lower = path["car_adjusted"] + 1.96 * path["se"], path["car_adjusted"] - 1.96 * path["se"]
        figure.add_trace(go.Scatter(x=list(path["offset"]) + list(path["offset"])[::-1], y=list(upper) + list(lower)[::-1],
                                    fill="toself", fillcolor=colors[kind], opacity=0.10, line={"width": 0},
                                    hoverinfo="skip", showlegend=False))
        figure.add_trace(go.Scatter(x=path["offset"], y=path["car_adjusted"], mode="lines+markers",
                                    name=f"{names[kind]}（{int(path['events'].iloc[0])} 個）",
                                    line={"color": colors[kind], "width": 2.6},
                                    hovertemplate="第 %{x} 天：%{y:+.2%}<extra></extra>"))
    figure.add_vline(x=0, line={"color": DOWN, "width": 2})
    figure.add_hline(y=0, line={"color": "#9aa39b", "width": 1})
    figure.add_annotation(x=0, y=1.02, yref="paper", text="熱搜當天", showarrow=False, font={"color": DOWN, "size": 13})
    figure.add_vrect(x0=0.5, x1=10.5, fillcolor="#f2f4ee", opacity=0.6, line_width=0, layer="below")
    figure.add_annotation(x=5.5, y=0.97, yref="paper", text="看到熱搜後才買進的區間", showarrow=False,
                          font={"color": "#5d665c", "size": 12})
    figure.update_layout(
        height=430, template="plotly_white", margin={"t": 30, "b": 10, "l": 10, "r": 10},
        legend={"orientation": "h", "y": -0.18}, yaxis_tickformat="+.1%",
        xaxis={"title": "相對熱搜當天的交易日（負數 = 熱搜之前）", "dtick": 1},
        yaxis={"title": "累積超額報酬（已扣大盤與平常水準）"},
    )
    return figure


def radar_chart(radar: pd.DataFrame) -> go.Figure:
    """Heat change vs basket return per theme; bubble size = news count."""
    radar = radar.dropna(subset=["heat_change", "return_10d"])
    figure = go.Figure(go.Scatter(
        x=radar["heat_change"], y=radar["return_10d"], mode="markers+text", text=radar["theme"],
        textposition="top center", marker={"size": (radar["news"].clip(lower=1) ** 0.5) * 2.2 + 10,
                                          "color": [UP if v > 0 else DOWN for v in radar["return_10d"]], "opacity": 0.75},
        customdata=radar[["news"]],
        hovertemplate="%{text}<br>熱度變化 %{x:+.0%}<br>同類股近 10 日 %{y:+.1%}<br>近 7 日新聞 %{customdata[0]} 則<extra></extra>",
    ))
    figure.add_hline(y=0, line={"color": "#9aa39b"})
    figure.add_vline(x=0, line={"color": "#9aa39b"})
    figure.update_layout(
        height=430, template="plotly_white", margin={"t": 20, "b": 10, "l": 10, "r": 10},
        xaxis={"title": "搜尋熱度變化（最近 7 天 vs 前 7 天）", "tickformat": "+.0%"},
        yaxis={"title": "同類股近 10 日平均漲跌", "tickformat": "+.0%"},
    )
    return figure


def schedule_chart() -> go.Figure:
    """One day of scheduled tasks (Asia/Taipei)."""
    day = datetime(2026, 1, 1)
    tasks = [
        ("RSS 熱搜收集", [day + timedelta(minutes=m) for m in range(14, 24 * 60, 20)], 2, HEAT),
        ("每日 ETL（AI 分類、熱度）", [day + timedelta(hours=h) for h in (2, 8, 14, 20)], 25, "#7b5ea7"),
        ("資料庫雲端備份", [day + timedelta(hours=3, minutes=30)], 6, "#2a9d8f"),
        ("健康報告", [day + timedelta(hours=7, minutes=30)], 6, ACCENT),
        ("股價收集（交易日）", [day + timedelta(hours=18, minutes=30)], 6, UP),
    ]
    figure = go.Figure()
    for row, (name, starts, minutes, color) in enumerate(tasks):
        for start in starts:
            figure.add_trace(go.Scatter(
                x=[start, start + timedelta(minutes=minutes)], y=[name, name], mode="lines",
                line={"color": color, "width": 18 if minutes > 2 else 10}, showlegend=False,
                hovertemplate=f"{name}<br>{start:%H:%M}<extra></extra>",
            ))
    figure.update_layout(
        height=300, template="plotly_white", margin={"t": 10, "b": 10, "l": 10, "r": 10},
        xaxis={"tickformat": "%H:%M", "dtick": 2 * 3600 * 1000, "range": [day, day + timedelta(days=1)], "title": "一天 24 小時（台北時間）"},
        yaxis={"autorange": "reversed"},
    )
    return figure


def return_breakdown_chart(raw: float, excess: float, abnormal: float, label: str) -> go.Figure:
    steps = ["原始報酬", "扣掉同期 0050", "再扣掉平常水準"]
    values = [raw, excess, abnormal]
    figure = go.Figure(go.Bar(
        x=steps, y=values, text=[f"{v:+.1%}" for v in values], textposition="outside",
        marker_color=[NEUTRAL, HEAT, UP if abnormal > 0 else DOWN],
    ))
    figure.update_layout(
        height=330, template="plotly_white", margin={"t": 40, "b": 10, "l": 10, "r": 10},
        title={"text": f"{label}：事件後 10 日平均報酬，三種算法", "font": {"size": 14}},
        yaxis={"tickformat": "+.0%", "title": "平均報酬"},
    )
    return figure
