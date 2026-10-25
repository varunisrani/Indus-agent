// Files (P18-21; W52-f look): browse the host, make folders, upload (picker or drop), preview text,
// delete and download. Header: the current path as a badge and a refresh button. Body: the path
// row (GO / UPLOAD / CREATE), a dashed drop zone and a grid table of the folder.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowUp,
  Download,
  File as FileIcon,
  Folder,
  FolderOpen,
  FolderPlus,
  LoaderCircle,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import { type DragEvent, type FormEvent, useEffect, useId, useRef, useState } from "react";
import { api, downloadFile, joinPath } from "../lib/api.ts";
import { formatDateTime, formatSize } from "../lib/format.ts";
import type { FileEntry } from "../lib/restTypes.ts";
import { HeaderActions, HeaderAfterTitle } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Button, Field, IconButton, Input, QueryState } from "../ui/controls.tsx";
import { cx } from "../ui/cx.ts";
import { ConfirmDialog, DialogActions, Modal } from "../ui/Dialog.tsx";

/** Start a download; a refusal (gone, too large, secret) shows as a toast. */
function useDownload(): (path: string) => void {
  const toast = useToast();
  return (path) => {
    downloadFile(path).catch((error: unknown) => toast.show(errorText(error), "error"));
  };
}

const HEAD = "text-[0.75rem] font-semibold tracking-[0.08em] text-ink-soft uppercase";

function Preview({ path, onClose }: { path: string; onClose: () => void }) {
  const file = useQuery({ queryKey: ["files", "read", path], queryFn: () => api.files.read(path) });
  const download = useDownload();
  return (
    <Modal
      open
      size="lg"
      onOpenChange={(next) => (next ? undefined : onClose())}
      title={path.split("/").pop() ?? path}
      description={path}
    >
      <QueryState loading={file.isPending} error={file.error}>
        {file.data?.text === null || file.data?.text === undefined ? (
          <p className="m-0 text-[0.875rem] text-ink-muted">
            This file is not text. Download it to open it.
          </p>
        ) : (
          <pre className="m-0 max-h-[60vh] overflow-auto border border-line bg-canvas p-3 font-mono text-[0.75rem] whitespace-pre-wrap text-ink">
            {file.data.text}
          </pre>
        )}
        {file.data?.truncated ? (
          <p className="mt-2 mb-0 text-[0.75rem] text-ink-muted">
            Only the start of the file is shown.
          </p>
        ) : null}
      </QueryState>
      <DialogActions>
        <Button icon={<Download />} onClick={() => download(path)}>
          Download
        </Button>
        <Button tone="primary" onClick={onClose}>
          Close
        </Button>
      </DialogActions>
    </Modal>
  );
}

function CreateFolder({
  here,
  onClose,
  onCreated,
}: {
  here: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const toast = useToast();
  const id = useId();
  const [name, setName] = useState("");
  const mkdir = useMutation({
    mutationFn: (folder: string) => api.files.mkdir(joinPath(here, folder)),
    onSuccess: () => {
      toast.show("Folder created", "success");
      onCreated();
      onClose();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const folder = name.trim();
    if (!folder || folder.includes("/") || folder === "." || folder === "..") {
      toast.show("Folder names cannot contain / or be . or ..", "error");
      return;
    }
    mkdir.mutate(folder);
  };
  return (
    <Modal
      open
      size="sm"
      onOpenChange={(next) => (next ? undefined : onClose())}
      title="Create folder"
      description={`Target: ${here}`}
    >
      <form onSubmit={submit} noValidate>
        <Field label="Folder name" htmlFor={id}>
          <Input id={id} autoFocus value={name} onChange={(e) => setName(e.currentTarget.value)} />
        </Field>
        <DialogActions>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            type="submit"
            tone="primary"
            icon={mkdir.isPending ? <LoaderCircle className="animate-spin" /> : <FolderPlus />}
            disabled={!name.trim() || mkdir.isPending}
          >
            Create
          </Button>
        </DialogActions>
      </form>
    </Modal>
  );
}

/** One folder entry: icon + name (opens or previews), size, date, and its icon actions. */
function FileRow({
  entry,
  last,
  onOpen,
  onDownload,
  onDelete,
}: {
  entry: FileEntry;
  last: boolean;
  onOpen: () => void;
  onDownload: () => void;
  onDelete: () => void;
}) {
  const Icon = entry.is_directory ? Folder : FileIcon;
  return (
    <tr
      data-testid={`file-${entry.name}`}
      className={cx("h-11 hover:bg-hover", !last && "border-b border-line/60")}
    >
      <td className="min-w-0 pl-4">
        <button
          type="button"
          aria-label={entry.is_directory ? `${entry.name}/` : entry.name}
          className="flex max-w-full items-center gap-2 text-left"
          onClick={onOpen}
        >
          <Icon
            aria-hidden="true"
            className={cx("size-4 shrink-0", entry.is_directory ? "text-warning" : "text-ink-soft")}
          />
          <span className="truncate font-mono text-[0.875rem] text-ink">{entry.name}</span>
        </button>
      </td>
      <td className="pl-3 text-[0.75rem] text-ink-muted tabular-nums">
        {entry.is_directory ? "-" : formatSize(entry.size)}
      </td>
      <td className="pl-3 text-[0.75rem] text-ink-muted">{formatDateTime(entry.mtime)}</td>
      <td className="pr-4">
        <div className="flex justify-end gap-1">
          {entry.is_directory ? (
            <IconButton label={`Open ${entry.name}`} onClick={onOpen}>
              <FolderOpen />
            </IconButton>
          ) : (
            <IconButton label={`Download ${entry.name}`} onClick={onDownload}>
              <Download />
            </IconButton>
          )}
          <IconButton tone="danger" label={`Delete ${entry.name}`} onClick={onDelete}>
            <Trash2 />
          </IconButton>
        </div>
      </td>
    </tr>
  );
}

export function FilesPage() {
  const toast = useToast();
  const download = useDownload();
  const client = useQueryClient();
  const picker = useRef<HTMLInputElement>(null);
  const [path, setPath] = useState<string | undefined>(undefined);
  const [draft, setDraft] = useState("");
  const [creating, setCreating] = useState(false);
  const [previewing, setPreviewing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<FileEntry | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);

  const listing = useQuery({
    queryKey: ["files", "list", path ?? ""],
    queryFn: () => api.files.list(path),
  });
  const here = listing.data?.path ?? path ?? "";
  const refresh = () => void client.invalidateQueries({ queryKey: ["files", "list"] });
  useEffect(() => setDraft(here), [here]);

  const remove = useMutation({
    mutationFn: (entry: FileEntry) => api.files.remove(entry.path),
    onSuccess: (_reply, entry) => {
      setDeleting(null);
      toast.show(`Deleted ${entry.name}.`, "success");
      refresh();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const upload = async (files: File[]) => {
    if (files.length === 0 || !here) return;
    setUploading(true);
    try {
      for (const file of files) await api.files.upload(here, file);
      toast.show(`${files.length} file${files.length === 1 ? "" : "s"} uploaded.`, "success");
      refresh();
    } catch (error) {
      toast.show(errorText(error), "error");
    } finally {
      setUploading(false);
    }
  };

  const go = (event: FormEvent) => {
    event.preventDefault();
    const next = draft.trim();
    if (!next) {
      toast.show("Path required", "error");
      return;
    }
    if (next === here) refresh();
    else setPath(next);
  };

  const hasFiles = (event: DragEvent) => [...event.dataTransfer.types].includes("Files");
  const dropDisabled = uploading || !here;
  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    if (dropDisabled) return;
    void upload([...event.dataTransfer.files]);
  };

  const entries = [...(listing.data?.entries ?? [])].sort(
    (a, b) => Number(b.is_directory) - Number(a.is_directory) || a.name.localeCompare(b.name),
  );
  const shownPath = listing.data?.locked_root ?? here;
  const parent = listing.data?.parent ?? null;

  return (
    <div className="flex flex-col gap-4 px-[1.4375rem] py-[1.4375rem]">
      <HeaderAfterTitle>
        <span
          data-testid="current-path"
          title={here}
          className="type-badge inline-flex h-6 max-w-[22rem] items-center truncate border border-line-strong px-2 text-ink-soft"
        >
          {shownPath}
        </span>
      </HeaderAfterTitle>
      <HeaderActions>
        <IconButton label="Refresh files" onClick={refresh} disabled={listing.isFetching}>
          <RefreshCw className={listing.isFetching ? "animate-spin" : undefined} />
        </IconButton>
      </HeaderActions>

      <form className="flex flex-wrap items-center gap-2" onSubmit={go} noValidate>
        <input
          aria-label="Path"
          className="type-data h-[2.125rem] min-w-0 flex-1 border border-line bg-canvas px-3 text-[0.875rem] text-ink outline-none focus:border-accent"
          value={draft}
          onChange={(e) => setDraft(e.currentTarget.value)}
          spellCheck={false}
        />
        <Button type="submit" size="sm" className="uppercase">
          Go
        </Button>
        <span className="w-1" />
        <Button
          size="sm"
          className="uppercase"
          icon={uploading ? <LoaderCircle className="animate-spin" /> : <Upload />}
          onClick={() => picker.current?.click()}
          disabled={dropDisabled}
        >
          Upload
        </Button>
        <Button
          size="sm"
          className="uppercase"
          icon={<FolderPlus />}
          onClick={() => setCreating(true)}
          disabled={!here}
        >
          Create
        </Button>
        <input
          ref={picker}
          type="file"
          multiple
          className="sr-only"
          tabIndex={-1}
          disabled={dropDisabled}
          aria-label="Upload files"
          onChange={(e) => {
            const files = [...(e.currentTarget.files ?? [])];
            e.currentTarget.value = "";
            void upload(files);
          }}
        />
      </form>

      <button
        type="button"
        data-testid="drop-zone"
        disabled={dropDisabled}
        onClick={() => picker.current?.click()}
        onDragEnter={(e) => {
          if (hasFiles(e)) setDragging(true);
        }}
        onDragOver={(e) => {
          if (hasFiles(e)) e.preventDefault();
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cx(
          "flex w-full items-center justify-between gap-4 border border-dashed px-4 py-3 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60",
          dragging ? "border-ink bg-ink/10" : "border-line-strong bg-surface/50 hover:bg-hover",
        )}
      >
        <span className="flex min-w-0 items-center gap-3">
          <span className="inline-flex size-[2.125rem] shrink-0 items-center justify-center border border-line-strong text-ink-soft">
            {uploading ? (
              <LoaderCircle aria-hidden="true" className="size-4 animate-spin" />
            ) : (
              <Upload aria-hidden="true" className="size-4" />
            )}
          </span>
          <span className="flex min-w-0 flex-col">
            <span className="text-[0.875rem] font-semibold tracking-[0.08em] text-ink uppercase">
              {uploading ? "Uploading" : dragging ? "Release to upload" : "Drop files here"}
            </span>
            <span className="truncate font-mono text-[0.75rem] text-ink-muted">{here}</span>
          </span>
        </span>
        <span className="hidden shrink-0 text-[0.75rem] font-semibold tracking-[0.08em] text-ink-soft uppercase sm:inline">
          Choose files
        </span>
      </button>

      <section aria-label="Folder contents" className="overflow-x-auto border border-line">
        {listing.error ? (
          <div
            role="alert"
            className="flex min-w-[42rem] items-start gap-2 border-b border-danger/20 bg-danger/10 p-3 text-[0.875rem] text-danger"
          >
            <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
            {errorText(listing.error)}
          </div>
        ) : null}
        <table aria-label="Files" className="w-full min-w-[42rem] table-fixed border-collapse">
          <colgroup>
            <col />
            <col className="w-[7.75rem]" />
            <col className="w-[10.75rem]" />
            <col className="w-[7.25rem]" />
          </colgroup>
          <thead>
            <tr className="h-[1.9375rem] border-b border-line">
              <th className={cx(HEAD, "pl-4 text-left")}>Name</th>
              <th className={cx(HEAD, "pl-3 text-left")}>Size</th>
              <th className={cx(HEAD, "pl-3 text-left")}>Modified</th>
              <th className={cx(HEAD, "pr-4 text-right")}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {parent ? (
              <tr className="h-[2.1875rem] border-b border-line/60">
                <td colSpan={4} className="h-[2.1875rem] p-0">
                  <button
                    type="button"
                    aria-label="Parent folder"
                    onClick={() => setPath(parent)}
                    className="flex h-full w-full items-center gap-2 px-4 text-left text-ink-muted hover:bg-hover"
                  >
                    <ArrowUp aria-hidden="true" className="size-4 text-ink-soft" />
                    <span className="font-mono text-[0.875rem]">..</span>
                  </button>
                </td>
              </tr>
            ) : null}
            {listing.isPending ? (
              <tr>
                <td colSpan={4} className="py-12">
                  <p className="m-0 flex items-center justify-center gap-2 text-[0.875rem] text-ink-muted">
                    <LoaderCircle aria-hidden="true" className="size-4 animate-spin" />
                    Loading files…
                  </p>
                </td>
              </tr>
            ) : !listing.error && entries.length === 0 ? (
              <tr>
                <td colSpan={4} className="py-12 text-center text-[0.875rem] text-ink-muted">
                  This folder is empty.
                </td>
              </tr>
            ) : (
              entries.map((entry, i) => (
                <FileRow
                  key={entry.path}
                  entry={entry}
                  last={i === entries.length - 1}
                  onOpen={() =>
                    entry.is_directory ? setPath(entry.path) : setPreviewing(entry.path)
                  }
                  onDownload={() => download(entry.path)}
                  onDelete={() => setDeleting(entry)}
                />
              ))
            )}
          </tbody>
        </table>
      </section>

      {creating ? (
        <CreateFolder here={here} onClose={() => setCreating(false)} onCreated={refresh} />
      ) : null}
      {previewing ? <Preview path={previewing} onClose={() => setPreviewing(null)} /> : null}
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete ${deleting?.name ?? ""}?`}
        description={
          deleting?.is_directory
            ? "The folder and everything in it is deleted from disk."
            : "The file is deleted from disk."
        }
        confirmLabel="Delete"
        danger
        busy={remove.isPending}
        onConfirm={() => {
          if (deleting) remove.mutate(deleting);
        }}
        onCancel={() => setDeleting(null)}
      />
    </div>
  );
}
