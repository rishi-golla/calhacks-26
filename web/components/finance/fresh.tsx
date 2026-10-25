"use client";
/**
 * "Fresh row" flashing: a row flashes once when it mounts after the page has
 * settled (a new live item, or a status change that remounts it). Rows present
 * on first render never flash, and adding the class later never re-triggers.
 */
import { useEffect, useState, type ComponentProps } from "react";

/** False for the first `ms`, then true. */
export function useSettled(ms = 1500) {
  const [settled, setSettled] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(true), ms);
    return () => clearTimeout(timer);
  }, [ms]);
  return settled;
}

/** Captures `settled` at mount time only. */
export function useFresh(settled: boolean) {
  const [fresh] = useState(settled);
  return fresh;
}

export function FreshTr({ settled, className = "", ...props }: ComponentProps<"tr"> & { settled: boolean }) {
  const fresh = useFresh(settled);
  return <tr className={`${className} ${fresh ? "fh-fresh" : ""}`} {...props} />;
}
