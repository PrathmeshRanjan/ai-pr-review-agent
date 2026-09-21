"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { useParams } from "next/navigation";
import clsx from "clsx";
import { FindingsByAgent } from "@/components/AgentFindingCard";
import { ReviewStatusBadge } from "@/components/ReviewStatusBadge";
import { VerdictChip } from "@/components/VerdictChip";
import { Empty } from "@/components/Empty";
import { api } from "@/lib/api";
import type { ReviewDetail, Finding } from "@/lib/types";

const AGENT_FILTERS = [
  { id: "all", label: "All Agents" },
  { id: "security", label: "Security" },
  { id: "quality", label: "Quality" },
  { id: "test", label: "Tests" },
  { id: "docs", label: "Docs" },
] as const;

const SEVERITY_FILTERS = [
  { id: "all", label: "All Severities" },
  { id: "critical", label: "Critical" },
  { id: "high", label: "High" },
  { id: "medium", label: "Medium" },
  { id: "low", label: "Low" },
  { id: "info", label: "Info" },
] as const;

export default function ReviewDetailPage() {
  const params = useParams<{ id: string }>();
  const id = decodeURIComponent(params.id);

  const [selectedAgent, setSelectedAgent] = useState<string>("all");
  const [selectedSeverity, setSelectedSeverity] = useState<string>("all");
  const [copied, setCopied] = useState(false);

  // Use api.getReview (custom fetcher) instead of the URL-string fetcher so
  // the BE-route fallback in lib/api.ts kicks in for ids containing "/".
  const { data, error, isLoading } = useSWR<ReviewDetail>(
    ["review-detail", id],
    () => api.getReview(id),
    { refreshInterval: 5000 }
  );

  const filteredFindings = useMemo(() => {
    if (!data?.findings) return [];
    return data.findings.filter((f: Finding) => {
      if (selectedAgent !== "all") {
        const agentLower = (f.agent_type ?? "").toLowerCase();
        if (!agentLower.includes(selectedAgent)) return false;
      }
      if (selectedSeverity !== "all") {
        const sevLower = (f.severity ?? "").toLowerCase();
        if (sevLower !== selectedSeverity) return false;
      }
      return true;
    });
  }, [data?.findings, selectedAgent, selectedSeverity]);

  const copyAsMarkdown = async () => {
    if (!data) return;

    let md = `# Review for ${data.repo_full_name} #${data.pr_number}\n\n`;
    md += `- **Verdict:** ${data.verdict ?? "Pending"}\n`;
    md += `- **Status:** ${data.status}\n`;
    if (data.overall_confidence != null) {
      md += `- **Confidence:** ${(data.overall_confidence * 100).toFixed(0)}%\n`;
    }
    md += `- **Commit:** \`${data.head_commit_sha}\`\n\n`;

    if (data.human_review_reason) {
      md += `> **Human Review Reason:** ${data.human_review_reason}\n\n`;
    }

    const findingsToExport = filteredFindings.length > 0 ? filteredFindings : (data.findings ?? []);
    md += `## Findings (${findingsToExport.length})\n\n`;

    if (findingsToExport.length === 0) {
      md += `No findings reported.\n`;
    } else {
      for (const f of findingsToExport) {
        const location = f.file_path
          ? ` \`${f.file_path}${f.line_start ? `:${f.line_start}` : ""}${f.line_end && f.line_end !== f.line_start ? `-${f.line_end}` : ""}\``
          : "";
        md += `### [${(f.agent_type ?? "AGENT").toUpperCase()}] ${f.summary}\n`;
        md += `- **Severity:** ${(f.severity ?? "INFO").toUpperCase()}\n`;
        if (location) md += `- **Location:**${location}\n`;
        md += `- **Confidence:** ${((f.confidence ?? 0) * 100).toFixed(0)}%\n`;
        if (f.suggestion) {
          md += `- **Suggestion:**\n  \`\`\`\n  ${f.suggestion}\n  \`\`\`\n`;
        }
        md += `\n`;
      }
    }

    try {
      await navigator.clipboard.writeText(md);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy markdown to clipboard", err);
    }
  };

  if (error) return <Empty>Failed to load: {error.message}</Empty>;
  if (isLoading || !data) return <Empty>Loading…</Empty>;

  const prUrl = `https://github.com/${data.repo_full_name}/pull/${data.pr_number}`;
  const totalFindings = data.findings?.length ?? 0;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="text-xs text-muted font-mono truncate">{data.id}</div>
          <h1 className="text-2xl font-semibold mt-1">
            {data.repo_full_name} #{data.pr_number}
          </h1>
          <div className="text-sm text-muted mt-1">{data.pr_title}</div>
          <div className="text-xs text-muted font-mono mt-2">
            {data.head_commit_sha}
          </div>
          <a
            href={prUrl}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-accent underline mt-2 inline-block"
          >
            Open PR on GitHub →
          </a>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <ReviewStatusBadge status={data.status} />
          <VerdictChip verdict={data.verdict} />
        </div>
      </div>

      {data.overall_confidence != null && (
        <div className="text-xs text-muted">
          Overall confidence: {(data.overall_confidence * 100).toFixed(0)}%
        </div>
      )}

      {/* Filter toolbar and Copy Markdown */}
      <div className="border border-border rounded-lg bg-panel p-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-border">
          <div className="text-sm font-medium">
            Filter Findings
            <span className="ml-2 text-xs text-muted font-normal font-mono">
              Showing {filteredFindings.length} of {totalFindings}
            </span>
          </div>

          <button
            onClick={copyAsMarkdown}
            type="button"
            className={clsx(
              "inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md transition-colors",
              copied
                ? "bg-ok/20 text-ok border border-ok/40"
                : "bg-bg hover:bg-border/60 border border-border text-foreground hover:text-white"
            )}
          >
            {copied ? (
              <>
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M5 13l4 4L19 7" />
                </svg>
                Copied Markdown!
              </>
            ) : (
              <>
                <svg className="w-3.5 h-3.5 text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                </svg>
                Copy as Markdown
              </>
            )}
          </button>
        </div>

        {/* Agent Filter Chips */}
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-muted font-mono uppercase tracking-wider w-16">Agent:</span>
          {AGENT_FILTERS.map((f) => {
            const active = selectedAgent === f.id;
            return (
              <button
                key={f.id}
                type="button"
                onClick={() => setSelectedAgent(f.id)}
                className={clsx(
                  "px-2.5 py-1 rounded text-xs font-mono transition-colors",
                  active
                    ? "bg-accent text-bg font-semibold"
                    : "bg-bg text-muted hover:text-foreground border border-border/50 hover:border-border"
                )}
              >
                {f.label}
              </button>
            );
          })}
        </div>

        {/* Severity Filter Chips */}
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-muted font-mono uppercase tracking-wider w-16">Severity:</span>
          {SEVERITY_FILTERS.map((f) => {
            const active = selectedSeverity === f.id;
            return (
              <button
                key={f.id}
                type="button"
                onClick={() => setSelectedSeverity(f.id)}
                className={clsx(
                  "px-2.5 py-1 rounded text-xs font-mono uppercase tracking-wide transition-colors",
                  active
                    ? "bg-accent text-bg font-semibold"
                    : "bg-bg text-muted hover:text-foreground border border-border/50 hover:border-border"
                )}
              >
                {f.label}
              </button>
            );
          })}
        </div>
      </div>

      {filteredFindings.length === 0 && totalFindings > 0 ? (
        <div className="border border-border rounded-lg bg-panel p-6 text-center text-sm text-muted">
          No findings match the selected filters.{" "}
          <button
            type="button"
            onClick={() => {
              setSelectedAgent("all");
              setSelectedSeverity("all");
            }}
            className="text-accent underline ml-1"
          >
            Reset filters
          </button>
        </div>
      ) : (
        <FindingsByAgent findings={filteredFindings} />
      )}

      {data.human_review_reason && (
        <div className="border border-border rounded-lg bg-panel p-4">
          <div className="text-xs uppercase tracking-wide text-muted mb-2">
            Human review reason
          </div>
          <p className="text-sm whitespace-pre-wrap">{data.human_review_reason}</p>
        </div>
      )}
    </div>
  );
}
