"use client";

import { useState } from "react";
import { LoaderCircle } from "lucide-react";
import "./agent-screen.css";

type ScreenProps = {
  streamSrc?: string;
  loading?: boolean;
  className?: string;
  alt?: string;
};

export function LoadingScreen() {
  return (
    <div className="agent-screen-loading" role="status">
      <div className="agent-screen-loading-content">
        <LoaderCircle
          className="agent-screen-spinner"
          size={26}
          aria-hidden="true"
        />
        <span>Connecting to agent&apos;s screen</span>
      </div>
    </div>
  );
}

function StreamImage({
  src,
  className,
  alt = "",
}: {
  src: string;
  className: string;
  alt?: string;
}) {
  const [ready, setReady] = useState(false);
  return (
    <>
      {src && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={src}
          alt={alt}
          className={className}
          onLoad={() => setReady(true)}
          onError={() => setReady(false)}
        />
      )}
      {!ready && <LoadingScreen />}
    </>
  );
}

export function Screen({
  streamSrc,
  loading = false,
  className = "",
  alt = "",
}: ScreenProps) {
  return (
    <div className={`agent-screen agent-screen-cover ${className}`}>
      {loading ? (
        <LoadingScreen />
      ) : (
        <StreamImage
          src={streamSrc ?? ""}
          className="agent-screen-image"
          alt={alt}
        />
      )}
    </div>
  );
}

export function MediaSizer({
  src,
  loading = false,
  className = "",
  alt = "",
}: {
  src: string;
  loading?: boolean;
  className?: string;
  alt?: string;
}) {
  return (
    <div className={`agent-screen agent-screen-contained ${className}`}>
      {loading ? (
        <LoadingScreen />
      ) : (
        <StreamImage
          src={src}
          className="agent-screen-image agent-screen-image-contained"
          alt={alt}
        />
      )}
    </div>
  );
}

export default function AgentScreen({
  streamSrc,
  loading = false,
  fit = "cover",
  className = "",
  alt = "",
}: ScreenProps & { fit?: "cover" | "contain" }) {
  return fit === "contain" ? (
    <MediaSizer
      src={streamSrc ?? ""}
      loading={loading}
      className={className}
      alt={alt}
    />
  ) : (
    <Screen
      streamSrc={streamSrc}
      loading={loading}
      className={className}
      alt={alt}
    />
  );
}
