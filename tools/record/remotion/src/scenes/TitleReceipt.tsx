import React from "react";
import { AbsoluteFill } from "remotion";
import { Background } from "../design/Background";
import { Receipt, ReceiptLine } from "../design/Receipt";

const LINES: ReceiptLine[] = [
  { kind: "header", text: "RECEIPTS" },
  { kind: "sentence", text: "an investigation agent for Honeycomb that has to show its work" },
  { kind: "rule" },
  { kind: "item", label: "hypothesis", value: "query_id" },
  { kind: "item", label: "negation", value: "ran" },
  { kind: "item", label: "not checked", value: "listed" },
  { kind: "item", label: "confidence", value: "priced" },
  { kind: "rule", accent: true },
  { kind: "item", label: "TOTAL", value: "the answer, graded", accent: true },
  { kind: "footer", text: "github.com/eknuth/receipts" },
];

export const TitleReceipt: React.FC = () => (
  <AbsoluteFill>
    <Background />
    <Receipt lines={LINES} />
  </AbsoluteFill>
);
