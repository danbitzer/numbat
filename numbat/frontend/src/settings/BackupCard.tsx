// Backup & restore for the saved config. Export is a plain download of the
// server's /api/config/export (the exact on-disk numbat-config.json document, so
// an exported file, a filesystem backup and the .bak are interchangeable).
// Import PUTs the chosen file through the normal validation path and replaces
// ALL settings — including sections the form doesn't own (vacation) and
// config-file-only keys — so a restore is exact and a fresh install can be
// seeded from an old one.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ConfigValidationError, putConfig } from "@/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { refetchPlanUntilFresh } from "@/planRefresh";

/** The config object inside a settings file: either the exported/on-disk
 * document ({schema_version, config: {...}}) or a bare config object. */
function unwrapDocument(parsed: unknown): Record<string, unknown> | null {
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
  const obj = parsed as Record<string, unknown>;
  const inner = obj.config;
  if (typeof inner === "object" && inner !== null && !Array.isArray(inner)) {
    return inner as Record<string, unknown>;
  }
  return obj;
}

export function BackupCard({
  configured,
  onImported,
}: {
  configured: boolean;
  onImported: () => void;
}) {
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [pending, setPending] = useState<{ doc: Record<string, unknown>; name: string } | null>(
    null,
  );
  const [errors, setErrors] = useState<string[]>([]);
  const [imported, setImported] = useState(false);

  const importConfig = useMutation({
    mutationFn: putConfig,
    onSuccess: async () => {
      setPending(null);
      setImported(true);
      // Refetch before onImported: the parent remounts the form from the
      // config query, which must already hold the imported values.
      await queryClient.invalidateQueries({ queryKey: ["config"] });
      void refetchPlanUntilFresh(queryClient);
      onImported();
    },
    onError: (e) => {
      setPending(null);
      setErrors(
        e instanceof ConfigValidationError
          ? e.fieldErrors.map((err) => `${err.loc}: ${err.msg}`)
          : [String(e)],
      );
    },
  });

  const onFile = async (file: File) => {
    setImported(false);
    setErrors([]);
    let parsed: unknown;
    try {
      parsed = JSON.parse(await file.text());
    } catch {
      setErrors([`${file.name} is not valid JSON.`]);
      return;
    }
    const doc = unwrapDocument(parsed);
    if (doc === null) {
      setErrors([`${file.name} does not look like a Numbat settings file.`]);
      return;
    }
    setPending({ doc, name: file.name });
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Backup</CardTitle>
        <CardDescription>
          Export the saved settings as a JSON file, or import one to restore them — for example
          on a fresh install.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex flex-wrap gap-3">
          {configured ? (
            <Button type="button" variant="outline" asChild>
              <a href="./api/config/export" download>
                Export settings
              </a>
            </Button>
          ) : (
            <Button type="button" variant="outline" disabled>
              Export settings
            </Button>
          )}
          <Button type="button" variant="outline" onClick={() => fileInput.current?.click()}>
            Import settings…
          </Button>
          <input
            ref={fileInput}
            type="file"
            accept=".json,application/json"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = ""; // re-selecting the same file must re-fire
              if (file) void onFile(file);
            }}
          />
        </div>
        {imported && (
          <p className="text-muted-foreground text-sm">Imported — settings applied.</p>
        )}
        {errors.length > 0 && (
          <div className="text-destructive space-y-0.5 text-sm">
            <p>Not imported — the file failed validation:</p>
            {errors.map((msg) => (
              <p key={msg}>{msg}</p>
            ))}
          </div>
        )}
      </CardContent>

      <Dialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Import settings?</DialogTitle>
            <DialogDescription>
              Every setting will be replaced with the values in{" "}
              <span className="text-foreground font-medium">{pending?.name}</span> and applied
              immediately. Fields the file doesn't set revert to their defaults; the previous
              settings are kept on disk as numbat-config.json.bak.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setPending(null)}>
              Cancel
            </Button>
            <Button
              type="button"
              disabled={importConfig.isPending}
              onClick={() => pending && importConfig.mutate(pending.doc)}
            >
              {importConfig.isPending ? "Importing…" : "Import & apply"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
