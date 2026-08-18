"use client";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Shared Markdown renderer.
 *
 * Tailwind v4's reset strips all default heading/list/code styling, so raw
 * `<ReactMarkdown>` output renders as unstyled text. This wrapper supplies
 * compact, legible styling tuned for three narrow columns sitting side by
 * side — headings in particular stay small, since a finding's `###` heading
 * should not dominate a column that is a third of the screen wide.
 */
const components: Components = {
  h1: ({ children }) => (
    <h3 className="mt-4 mb-1.5 text-base font-semibold text-slate-100 first:mt-0">{children}</h3>
  ),
  h2: ({ children }) => (
    <h3 className="mt-4 mb-1.5 text-sm font-semibold text-slate-100 first:mt-0">{children}</h3>
  ),
  h3: ({ children }) => (
    <h3 className="mt-3.5 mb-1 text-sm font-semibold text-slate-100 first:mt-0">{children}</h3>
  ),
  h4: ({ children }) => <h4 className="mt-3 mb-1 text-xs font-semibold text-slate-200">{children}</h4>,
  h5: ({ children }) => <h5 className="mt-2 mb-1 text-xs font-semibold text-slate-300">{children}</h5>,
  h6: ({ children }) => <h6 className="mt-2 mb-1 text-xs font-semibold text-slate-400">{children}</h6>,
  p: ({ children }) => <p className="mb-2 text-sm leading-relaxed text-slate-300">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold text-slate-100">{children}</strong>,
  em: ({ children }) => <em className="text-slate-300">{children}</em>,
  ul: ({ children }) => <ul className="mb-2 ml-4 list-disc space-y-1 text-sm text-slate-300">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2 ml-4 list-decimal space-y-1 text-sm text-slate-300">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  code: ({ className, children, ...props }) => {
    const isBlock = /language-/.test(className ?? "");
    if (isBlock) {
      return (
        <code className={`font-mono text-xs ${className ?? ""}`} {...props}>
          {children}
        </code>
      );
    }
    return (
      <code
        className="rounded bg-slate-800 px-1 py-0.5 font-mono text-[0.85em] text-amber-300"
        {...props}
      >
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    <pre className="mb-2 overflow-x-auto rounded-md border border-slate-800 bg-slate-950 p-2.5 leading-snug">
      {children}
    </pre>
  ),
  blockquote: ({ children }) => (
    <blockquote className="mb-2 border-l-2 border-slate-600 pl-3 text-sm italic text-slate-400">
      {children}
    </blockquote>
  ),
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="text-blue-400 underline underline-offset-2 hover:text-blue-300"
    >
      {children}
    </a>
  ),
  hr: () => <hr className="my-3 border-slate-800" />,
  table: ({ children }) => (
    <div className="mb-2 overflow-x-auto">
      <table className="w-full text-left text-xs">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-slate-900">{children}</thead>,
  th: ({ children }) => (
    <th className="border-b border-slate-700 px-2 py-1 font-semibold text-slate-200">{children}</th>
  ),
  td: ({ children }) => <td className="border-b border-slate-800 px-2 py-1 align-top text-slate-300">{children}</td>,
};

export default function Markdown({ children }: { children: string }) {
  return (
    <div className="max-w-none">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
