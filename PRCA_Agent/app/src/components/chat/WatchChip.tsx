"use client";

interface WatchChipProps {
  jurisdictions: string[];
}

export function WatchChip({ jurisdictions }: WatchChipProps) {
  return (
    <div className="flex items-center gap-2">
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full bg-foreground opacity-75 animate-pulse" />
        <span className="relative inline-flex h-2 w-2 bg-foreground" />
      </span>
      <span className="text-xs text-muted font-mono tracking-wider uppercase">
        Watching:
        {jurisdictions.map((j) => (
          <span
            key={j}
            className="inline-block border border-border px-2 py-0.5 text-xs font-mono ml-1"
          >
            {j}
          </span>
        ))}
      </span>
    </div>
  );
}
