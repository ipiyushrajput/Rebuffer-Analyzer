/**
 * The DRM panel.
 *
 * This is the only screen in the product where anything about DRM is typed, and it is typed
 * once per deployment. After that a protected channel is pasted into Realtime, Aging, Bulk or
 * an automated batch exactly like a clear one: the backend detects the protection, obtains the
 * content key over CPIX, decrypts the segments before the bitstream rules read them, and
 * relays the player's licence request.
 *
 * The three CPIX credentials are a path on the analyzer host or an HTTPS URL, never key
 * material. What is typed here goes to the backend and is never sent back: the panel shows
 * whether each one is set, which is what an operator needs to know and all a page is told.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ApiError, endpoints, type DrmSettings } from "../api/client";
import { Card, CardHeader, Field, InlineAlert } from "./ui";
import { IconCheck } from "./ui/icons";

function message(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : String(error);
}

/** One credential source. Blank means "leave whatever is stored alone". */
interface Draft {
  license_url: string;
  cpix_endpoint: string;
  cpix_content_id: string;
  cpix_client_cert: string;
  cpix_client_key: string;
  cpix_server_cert: string;
}

const EMPTY: Draft = {
  license_url: "",
  cpix_endpoint: "",
  cpix_content_id: "",
  cpix_client_cert: "",
  cpix_client_key: "",
  cpix_server_cert: "",
};

function statusChip(drm: DrmSettings): { label: string; className: string } {
  if (!drm.enabled)
    return { label: "DRM switched off", className: "chip-neutral" };
  if (drm.keys_configured && drm.playback_configured)
    return {
      label: "Analysis and playback configured",
      className: "chip-clean",
    };
  if (drm.keys_configured)
    return { label: "Analysis only", className: "chip-violet" };
  if (drm.playback_configured)
    return { label: "Playback only", className: "chip-violet" };
  return { label: "Not configured", className: "chip-pink" };
}

export function DrmSettingsPanel() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [enabled, setEnabled] = useState(true);
  const [decryptEvidence, setDecryptEvidence] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const stored = useQuery({
    queryKey: ["drm-settings"],
    queryFn: endpoints.drmSettings,
  });
  const drm = stored.data?.drm;

  useEffect(() => {
    if (!drm) return;
    setEnabled(drm.enabled);
    setDecryptEvidence(drm.decrypt_evidence);
  }, [drm]);

  const save = useMutation({
    mutationFn: () => {
      // A field left blank is left out of the request, so saving the licence URL cannot blank
      // a credential this page was never shown.
      const body: Record<string, string | boolean> = {
        enabled,
        decrypt_evidence: decryptEvidence,
      };
      for (const [key, value] of Object.entries(draft)) {
        if (value.trim()) body[key] = value.trim();
      }
      return endpoints.saveDrmSettings(body);
    },
    onSuccess: () => {
      setError(null);
      setSaved(true);
      setDraft(EMPTY);
      window.setTimeout(() => setSaved(false), 2500);
      void queryClient.invalidateQueries({ queryKey: ["drm-settings"] });
    },
    onError: (err: Error) => setError(message(err)),
  });

  const chip = drm ? statusChip(drm) : null;
  const dirty =
    enabled !== (drm?.enabled ?? true) ||
    decryptEvidence !== (drm?.decrypt_evidence ?? false) ||
    Object.values(draft).some((v) => v.trim());

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="DRM"
          subtitle="Configured once for this deployment. After that a protected channel is analysed exactly like a clear one, with nothing to fill in per channel."
          actions={chip && <span className={chip.className}>{chip.label}</span>}
        />

        <div className="space-y-4 px-5 pb-5">
          {drm && (
            <dl className="grid gap-3 sm:grid-cols-3">
              <div>
                <dt className="field-label">Client certificate</dt>
                <dd className="font-mono text-small text-ink-soft">
                  {drm.client_cert_set ? "set" : "not set"}
                </dd>
              </div>
              <div>
                <dt className="field-label">Client private key</dt>
                <dd className="font-mono text-small text-ink-soft">
                  {drm.client_key_set ? "set" : "not set"}
                </dd>
              </div>
              <div>
                <dt className="field-label">Key server certificate</dt>
                <dd className="font-mono text-small text-ink-soft">
                  {drm.server_cert_set ? "set" : "not set"}
                </dd>
              </div>
            </dl>
          )}

          <label className="flex items-center gap-2 text-small text-ink-soft">
            <input
              type="checkbox"
              className="h-4 w-4"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            Analyse and play protected channels
          </label>
          {!enabled && (
            <InlineAlert tone="info">
              A protected channel is still polled and measured for transport,
              timing and playlist defects. Its report states, per rendition,
              that the payload was not read.
            </InlineAlert>
          )}

          {/*
            An evidence bundle already carries the protected segments as the CDN served them.
            This adds the decrypted copy beside them, which is what a packager needs to
            reproduce a bitstream defect — and is the content in the clear, in a file that
            gets forwarded. Off unless a deployment decides otherwise.
          */}
          <label className="flex items-center gap-2 text-small text-ink-soft">
            <input
              type="checkbox"
              className="h-4 w-4"
              checked={decryptEvidence}
              onChange={(e) => setDecryptEvidence(e.target.checked)}
              disabled={!enabled}
            />
            Include decrypted media in evidence bundles
          </label>
          {decryptEvidence && (
            <InlineAlert tone="info">
              Bundles will carry the decrypted segments alongside the encrypted
              ones, each with its initialisation segment in front of it. No
              content key is ever written to a bundle, a report or a log.
            </InlineAlert>
          )}

          <Field
            label="Widevine licence URL"
            htmlFor="drm-license"
            hint="The licence server the player acquires a licence from. The browser never reaches it: POST /api/drm/license relays the challenge from this host."
          >
            <input
              id="drm-license"
              className="input-mono"
              placeholder={
                drm?.license_url || "https://…/license?drm-type=widevine"
              }
              value={draft.license_url}
              onChange={(e) =>
                setDraft({ ...draft, license_url: e.target.value })
              }
              spellCheck={false}
              autoComplete="off"
            />
          </Field>

          <Field
            label="CPIX endpoint"
            htmlFor="drm-endpoint"
            hint="The key server the content keys are requested from."
          >
            <input
              id="drm-endpoint"
              className="input-mono"
              placeholder={drm?.endpoint || stored.data?.endpoint_default || ""}
              value={draft.cpix_endpoint}
              onChange={(e) =>
                setDraft({ ...draft, cpix_endpoint: e.target.value })
              }
              spellCheck={false}
              autoComplete="off"
            />
          </Field>

          <Field
            label="Client certificate"
            htmlFor="drm-client-cert"
            hint="A path on the analyzer host, or an HTTPS URL. The CPIX request is signed with it and the keys come back encrypted to it."
          >
            <input
              id="drm-client-cert"
              className="input-mono"
              placeholder={
                drm?.client_cert_set
                  ? "set — type to replace"
                  : "/etc/rba/public_cert.pem"
              }
              value={draft.cpix_client_cert}
              onChange={(e) =>
                setDraft({ ...draft, cpix_client_cert: e.target.value })
              }
              spellCheck={false}
              autoComplete="off"
            />
          </Field>

          <Field
            label="Client private key"
            htmlFor="drm-client-key"
            hint="A path on the analyzer host, or an HTTPS URL. Keep it behind authentication: whoever can read it can read every key this deployment obtains."
          >
            <input
              id="drm-client-key"
              className="input-mono"
              placeholder={
                drm?.client_key_set
                  ? "set — type to replace"
                  : "/etc/rba/private_key.pem"
              }
              value={draft.cpix_client_key}
              onChange={(e) =>
                setDraft({ ...draft, cpix_client_key: e.target.value })
              }
              spellCheck={false}
              autoComplete="off"
            />
          </Field>

          <Field
            label="Key server certificate"
            htmlFor="drm-server-cert"
            hint="The key server's own certificate. The answer's signature is checked against it, so a response signed by anything else is reported."
          >
            <input
              id="drm-server-cert"
              className="input-mono"
              placeholder={
                drm?.server_cert_set
                  ? "set — type to replace"
                  : "/etc/rba/keyos_cert.pem"
              }
              value={draft.cpix_server_cert}
              onChange={(e) =>
                setDraft({ ...draft, cpix_server_cert: e.target.value })
              }
              spellCheck={false}
              autoComplete="off"
            />
          </Field>

          <Field
            label="Content identifier"
            htmlFor="drm-content-id"
            hint="What a CPIX document names the content as. One value for the deployment; the key server keys on the key identifier, not on this."
          >
            <input
              id="drm-content-id"
              className="input-mono"
              placeholder={drm?.cpix_content_id || "rba"}
              value={draft.cpix_content_id}
              onChange={(e) =>
                setDraft({ ...draft, cpix_content_id: e.target.value })
              }
              spellCheck={false}
              autoComplete="off"
            />
          </Field>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn-primary"
              disabled={!dirty || save.isPending}
              onClick={() => save.mutate()}
            >
              {save.isPending ? "Saving" : "Save DRM settings"}
            </button>
            {saved && (
              <span className="inline-flex items-center gap-1 text-small font-semibold text-clean-600">
                <IconCheck size={14} />
                Stored
              </span>
            )}
            <span className="text-small text-ink-muted">
              A running job keeps the settings it started with.
            </span>
          </div>

          {error && <InlineAlert tone="error">{error}</InlineAlert>}
          {stored.isError && (
            <InlineAlert tone="error">{message(stored.error)}</InlineAlert>
          )}
        </div>
      </Card>
    </div>
  );
}
