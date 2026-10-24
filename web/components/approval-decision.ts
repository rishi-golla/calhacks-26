"use client";

import { useEffect, useRef, useState } from "react";
import { num, str, type HumanReducer, type Row } from "@/lib/data";

export type ApprovalDecision = "approved" | "rejected";

type AwaitingDecision = {
  id: string;
  decision: ApprovalDecision;
  attempts: number;
};

type UseApprovalDecisionOptions = {
  approval?: Row;
  approvals: Row[];
  mutate: (name: HumanReducer, payload: Row) => Promise<void>;
  onConfirmed?: (id: string, decision: ApprovalDecision) => void;
};

export function approvalError(value: unknown): string {
  if (value === "invalid_code")
    return "The one-time code is incorrect. Try again or request a fresh code from your administrator.";
  if (value === "code_locked")
    return "This approval code is locked after five failed attempts. Ask your administrator for a fresh code.";
  return str(
    value,
    "The code was rejected. Request a fresh code from your administrator.",
  );
}

export function useApprovalDecision({
  approval,
  approvals,
  mutate,
  onConfirmed,
}: UseApprovalDecisionOptions) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [awaiting, setAwaiting] = useState<AwaitingDecision | null>(null);
  const previousApprovalId = useRef("");
  const onConfirmedRef = useRef(onConfirmed);

  useEffect(() => {
    onConfirmedRef.current = onConfirmed;
  }, [onConfirmed]);

  useEffect(() => {
    const approvalId = str(approval?.id, "");
    if (previousApprovalId.current === approvalId) return;
    previousApprovalId.current = approvalId;
    setCode("");
    setError(approval?.last_error ? approvalError(approval.last_error) : "");
    setBusy(false);
    setAwaiting(null);
  }, [approval?.id, approval?.last_error]);

  useEffect(() => {
    if (!awaiting) return;
    const result = approvals.find((row) => row.id === awaiting.id);
    if (result?.decision === awaiting.decision) {
      onConfirmedRef.current?.(awaiting.id, awaiting.decision);
      setAwaiting(null);
      setBusy(false);
      setCode("");
    } else if (result && num(result.failed_attempts) > awaiting.attempts) {
      setError(approvalError(result.last_error));
      setAwaiting(null);
      setBusy(false);
      setCode("");
    }
  }, [approvals, awaiting]);

  useEffect(() => {
    if (!awaiting) return;
    const timer = setTimeout(() => {
      setBusy(false);
      setAwaiting(null);
      setError(
        "No decision was confirmed yet. Check the approval's live status before trying again.",
      );
    }, 12000);
    return () => clearTimeout(timer);
  }, [awaiting]);

  const decide = async (decision: ApprovalDecision) => {
    if (!approval || busy) return;
    const id = str(approval.id);
    setError("");
    setBusy(true);
    setAwaiting({
      id,
      decision,
      attempts: num(approval.failed_attempts),
    });
    try {
      await mutate("decide_approval", { id, decision, code });
    } catch (cause) {
      setAwaiting(null);
      setBusy(false);
      setError(String(cause).replace(/^Error: /, ""));
    }
  };

  return { code, setCode, busy, error, setError, decide };
}
