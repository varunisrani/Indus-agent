// What a routed page shows until its own task lands (P18-23..25). Swapped out row by row in
// routes.ts; nothing else imports it.

export function PendingPage({ title }: { title: string }) {
  return (
    <div className="p-6">
      <p className="text-sm text-muted">The {title} page is not part of this build yet.</p>
    </div>
  );
}
