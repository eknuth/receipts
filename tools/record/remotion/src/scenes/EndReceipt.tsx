import React from "react";
import { AbsoluteFill } from "remotion";
import { Background } from "../design/Background";
import { Receipt, ReceiptLine } from "../design/Receipt";

// The grader's price list. Every number here is copied from evals/grader.md
// verbatim, nothing typed from memory: high-confidence wrong -0.50, low
// wrong -0.10 (the milder of the two wrong penalties, medium being -0.25),
// medium right -0.05 (the milder of the two right-but-hedged penalties, low
// being -0.10), confident and right costs nothing.
const LINES: ReceiptLine[] = [
  { kind: "header", text: "RECEIPTS" },
  { kind: "item", label: "confident and wrong", value: "-0.50" },
  { kind: "item", label: "hedged and wrong", value: "-0.10" },
  { kind: "item", label: "hedged and right", value: "-0.05" },
  { kind: "item", label: "confident and right", value: "0.00", accent: true },
  { kind: "rule", accent: true },
  { kind: "sentence", text: "Every hypothesis cites its query and its negation." },
  { kind: "sentence", text: "The report lists what it never checked." },
  { kind: "footer", text: "github.com/eknuth/receipts" },
];

export const EndReceipt: React.FC = () => (
  <AbsoluteFill>
    <Background />
    <Receipt lines={LINES} />
  </AbsoluteFill>
);
