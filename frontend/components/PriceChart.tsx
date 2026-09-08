"use client";

import { useEffect, useRef } from "react";
import {
  createChart, ColorType, type IChartApi, type ISeriesApi, LineStyle,
} from "lightweight-charts";
import { api } from "@/lib/api";

type Level = { price: number; color: string; title: string };

type CandlePayload = {
  candles: { time: number; open: number; high: number; low: number; close: number }[];
  volume: { time: number; value: number; color: string }[];
  ema_50: { time: number; value: number }[];
  ema_200: { time: number; value: number }[];
  supertrend: { time: number; value: number }[];
};

/** TradingView tarzı mum grafiği (lightweight-charts). */
export function PriceChart({
  market,
  exchange,
  symbol,
  timeframe,
  levels = [],
  height = 420,
}: {
  market: string;
  exchange: string;
  symbol: string;
  timeframe: string;
  levels?: Level[];
  height?: number;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  useEffect(() => {
    if (!boxRef.current) return;

    const chart = createChart(boxRef.current, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#8fa3b0",
        fontFamily: "JetBrains Mono, monospace",
      },
      grid: {
        vertLines: { color: "rgba(148,163,184,.06)" },
        horzLines: { color: "rgba(148,163,184,.06)" },
      },
      crosshair: { mode: 0 },
      rightPriceScale: { borderColor: "rgba(148,163,184,.12)" },
      timeScale: { borderColor: "rgba(148,163,184,.12)", timeVisible: true },
    });
    chartRef.current = chart;

    const candles = chart.addCandlestickSeries({
      upColor: "#00e676",
      downColor: "#ff4d6d",
      borderUpColor: "#00e676",
      borderDownColor: "#ff4d6d",
      wickUpColor: "#00e676",
      wickDownColor: "#ff4d6d",
    });
    candleRef.current = candles;

    const volume = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });

    const ema50 = chart.addLineSeries({ color: "#38bdf8", lineWidth: 1 });
    const ema200 = chart.addLineSeries({ color: "#f0a5ff", lineWidth: 1 });
    const supertrend = chart.addLineSeries({
      color: "#00f59b", lineWidth: 1, lineStyle: LineStyle.Dashed,
    });

    let cancelled = false;
    (async () => {
      const data = await api<CandlePayload>(
        `/api/market/candles?market=${market}&exchange=${exchange}` +
          `&symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=300`,
      );
      if (cancelled) return;
      candles.setData(data.candles as never);
      volume.setData(data.volume as never);
      ema50.setData(data.ema_50 as never);
      ema200.setData(data.ema_200 as never);
      supertrend.setData(data.supertrend as never);
      chart.timeScale().fitContent();

      levels.forEach((level) =>
        candles.createPriceLine({
          price: level.price,
          color: level.color,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: level.title,
        }),
      );
    })();

    const resize = () =>
      chart.applyOptions({ width: boxRef.current?.clientWidth ?? 600 });
    resize();
    window.addEventListener("resize", resize);

    return () => {
      cancelled = true;
      window.removeEventListener("resize", resize);
      chart.remove();
    };
  }, [market, exchange, symbol, timeframe, height, levels]);

  return <div ref={boxRef} className="w-full" />;
}
