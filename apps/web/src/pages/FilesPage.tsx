// Files (P18-21): browse the host, make folders, upload, preview text, delete and download.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ChangeEvent, type FormEvent, useState } from "react";
import { api, downloadFile, joinPath } from "../lib/api.ts";
import { formatBytes, relativeTime } from "../lib/format.ts";
import type { FileEntry } from "../lib/restTypes.ts";
import { HeaderActions } from "../shell/PageHeader.tsx";
import { errorText, useToast } from "../shell/Toasts.tsx";
import { Button, Card, inputClass, QueryState } from "../ui/controls.tsx";
import { ConfirmDialog, Modal } from "../ui/Dialog.tsx";

/** Start a download; a refusal (gone, too large, secret) shows as a toast. */
function useDownload(): (path: string) => void {
  const toast = useToast();
  return (path) => {
    downloadFile(path).catch((error: unknown) => toast.show(errorText(error), "error"));
  };
}

function Preview({ path, onClose }: { path: string; onClose: () => void }) {
  const file = useQuery({ queryKey: ["files", "read", path], queryFn: () => api.files.read(path) });
  const download = useDownload();
  return (
    <Modal
      open
      onOpenChange={(next) => (next ? undefined : onClose())}
      title={path.split("/").pop() ?? path}
      description={path}
    >
      <QueryState loading={file.isPending} error={file.error}>
        {file.data?.text === null || file.data?.text === undefined ? (
          <p className="text-sm text-muted">This file is not text. Download it to open it.</p>
        ) : (
          <pre className="max-h-[60vh] overflow-auto rounded-md bg-code p-2 font-mono text-xs whitespace-pre-wrap">
            {file.data.text}
          </pre>
        )}
        {file.data?.truncated ? (
          <p className="mt-2 text-xs text-muted">Only the start of the file is shown.</p>
        ) : null}
      </QueryState>
      <div className="mt-3 flex justify-end gap-2">
        <Button onClick={() => download(path)}>Download</Button>
        <Button onClick={onClose}>Close</Button>
      </div>
    </Modal>
  );
}

export function FilesPage() {
  const toast = useToast();
  const download = useDownload();
  const client = useQueryClient();
  const [path, setPath] = useState<string | undefined>(undefined);
  const [folderName, setFolderName] = useState("");
  const [previewing, setPreviewing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<FileEntry | null>(null);
  const [uploading, setUploading] = useState(false);

  const listing = useQuery({
    queryKey: ["files", "list", path ?? ""],
    queryFn: () => api.files.list(path),
  });
  const here = listing.data?.path ?? path ?? "";
  const refresh = () => void client.invalidateQueries({ queryKey: ["files", "list"] });

  const mkdir = useMutation({
    mutationFn: (name: string) => api.files.mkdir(joinPath(here, name)),
    onSuccess: () => {
      setFolderName("");
      refresh();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });
  const remove = useMutation({
    mutationFn: (entry: FileEntry) => api.files.remove(entry.path),
    onSuccess: (_reply, entry) => {
      setDeleting(null);
      toast.show(`Deleted ${entry.name}.`, "success");
      refresh();
    },
    onError: (error) => toast.show(errorText(error), "error"),
  });

  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = [...(event.currentTarget.files ?? [])];
    event.currentTarget.value = "";
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

  const createFolder = (event: FormEvent) => {
    event.preventDefault();
    const name = folderName.trim();
    if (!name || name.includes("/") || name === "." || name === "..") {
      toast.show("Folder names cannot contain / or be . or ..", "error");
      return;
    }
    mkdir.mutate(name);
  };

  const entries = [...(listing.data?.entries ?? [])].sort(
    (a, b) => Number(b.is_directory) - Number(a.is_directory) || a.name.localeCompare(b.name),
  );

  return (
    <div className="flex flex-col gap-4 p-4">
      <HeaderActions>
        <label
          className={`inline-flex cursor-pointer items-center rounded-md border border-line bg-raised px-3 py-1.5 text-sm ${uploading ? "opacity-60" : ""}`}
        >
          {uploading ? "Uploading…" : "Upload"}
          <input
            type="file"
            multiple
            className="sr-only"
            disabled={uploading || !here}
            onChange={(e) => void upload(e)}
            aria-label="Upload files"
          />
        </label>
      </HeaderActions>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          disabled={!listing.data?.parent}
          onClick={() => listing.data?.parent && setPath(listing.data.parent)}
        >
          ↑ Up
        </Button>
        <code className="font-mono text-sm break-all" data-testid="current-path">
          {here}
        </code>
        {listing.data?.locked_root ? (
          <span className="text-xs text-muted">(limited to {listing.data.locked_root})</span>
        ) : null}
      </div>
      <form className="flex gap-2" onSubmit={createFolder}>
        <input
          aria-label="New folder name"
          placeholder="New folder"
          className={inputClass}
          value={folderName}
          onChange={(e) => setFolderName(e.currentTarget.value)}
        />
        <Button type="submit" disabled={!folderName.trim() || mkdir.isPending}>
          Create folder
        </Button>
      </form>
      <Card>
        <QueryState
          loading={listing.isPending}
          error={listing.error}
          empty={entries.length === 0}
          emptyText="This folder is empty."
        >
          <table className="w-full text-sm" aria-label="Files">
            <thead>
              <tr className="text-left text-xs text-muted">
                <th className="py-1">Name</th>
                <th className="py-1">Size</th>
                <th className="py-1">Changed</th>
                <th className="py-1">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr
                  key={entry.path}
                  className="border-t border-line"
                  data-testid={`file-${entry.name}`}
                >
                  <td className="py-1">
                    <button
                      type="button"
                      className="text-left"
                      onClick={() =>
                        entry.is_directory ? setPath(entry.path) : setPreviewing(entry.path)
                      }
                    >
                      {entry.name}
                      {entry.is_directory ? "/" : ""}
                    </button>
                  </td>
                  <td className="py-1 text-xs text-muted">
                    {entry.is_directory ? "" : formatBytes(entry.size)}
                  </td>
                  <td className="py-1 text-xs text-muted">
                    {entry.mtime === null ? "" : relativeTime(entry.mtime)}
                  </td>
                  <td className="flex justify-end gap-1 py-1">
                    {entry.is_directory ? null : (
                      <Button
                        tone="ghost"
                        onClick={() => download(entry.path)}
                        aria-label={`Download ${entry.name}`}
                      >
                        Download
                      </Button>
                    )}
                    <Button
                      tone="ghost"
                      onClick={() => setDeleting(entry)}
                      aria-label={`Delete ${entry.name}`}
                    >
                      Delete
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </QueryState>
      </Card>
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
