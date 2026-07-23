import { useEffect, useMemo, useState } from "react";
import { runtimeApi as api } from "@/controllers/API/runtime-api";
import i18n, { loadLanguage } from "@/i18n";

type SetupStatus = {
  complete: boolean;
  release_version: string | null;
  api_versions?: string[];
  branding?: {
    solution_name?: string;
    organization_name?: string;
    logo_url?: string | null;
    primary_color?: string;
    login_notice?: string;
    show_unnest_branding?: boolean;
  };
  default_language?: "ko" | "en";
  allow_language_switch?: boolean;
  license: { valid: boolean; reason: string | null };
  required_secret_names: string[];
  configured_secret_names: string[];
};

type SetupResult = SetupStatus & { recovery_identity?: string };
type RuntimeUser = {
  id: string;
  username: string;
  is_active?: boolean;
  is_superuser?: boolean;
};
type RuntimeDocument = {
  id: string;
  name: string;
  status: string;
  version_number: number;
  checksum: string;
  size_bytes: number;
  job_id?: string;
};
type RuntimeBackup = {
  id: string;
  checksum: string;
  size_bytes: number;
  created_at: string;
};
type RuntimeManagedUser = {
  id: string;
  username: string;
  role: "admin" | "general";
  is_active: boolean;
};
type RuntimeTab = "agent" | "documents" | "operations";

const errorMessage = (error: unknown) => {
  const response = error as {
    response?: { data?: { detail?: string } };
    message?: string;
  };
  return (
    response.response?.data?.detail ?? response.message ?? "Request failed"
  );
};

function LanguageButtons({ allowSwitch = true }: { allowSwitch?: boolean }) {
  const [language, setLanguage] = useState(i18n.language);
  useEffect(() => {
    const syncLanguage = (next: string) => setLanguage(next);
    i18n.on("languageChanged", syncLanguage);
    return () => {
      i18n.off("languageChanged", syncLanguage);
    };
  }, []);
  const changeLanguage = async (next: "ko" | "en") => {
    await loadLanguage(next);
    await i18n.changeLanguage(next);
    document.documentElement.lang = next;
    localStorage.setItem("languagePreference", next);
  };
  return allowSwitch ? (
    <div className="flex gap-1" aria-label="Language">
      {(["ko", "en"] as const).map((code) => (
        <button
          key={code}
          type="button"
          className={`rounded border px-2 py-1 text-xs ${
            language.startsWith(code)
              ? "bg-primary text-primary-foreground"
              : ""
          }`}
          onClick={() => changeLanguage(code)}
          aria-pressed={language.startsWith(code)}
        >
          {code === "ko" ? "한국어" : "English"}
        </button>
      ))}
    </div>
  ) : null;
}

function UnnestMark({ status }: { status: SetupStatus }) {
  return status.branding?.show_unnest_branding !== false ? (
    <p className="text-xs text-muted-foreground">Powered by Unnest</p>
  ) : null;
}

function RuntimeLogin({
  status,
  onLogin,
}: {
  status: SetupStatus;
  onLogin: (user: RuntimeUser) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [isPending, setIsPending] = useState(false);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    setIsPending(true);
    try {
      await api.post(
        "/api/v1/login",
        new URLSearchParams({
          username: username.trim(),
          password,
        }).toString(),
        { headers: { "Content-Type": "application/x-www-form-urlencoded" } },
      );
      const session = await api.get<{
        authenticated: boolean;
        user?: RuntimeUser;
      }>("/api/v1/session");
      if (!session.data.authenticated || !session.data.user) {
        throw new Error("Login session was not established");
      }
      onLogin(session.data.user);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setIsPending(false);
    }
  };
  return (
    <main className="flex min-h-screen items-center justify-center bg-muted p-6">
      <form
        className="w-full max-w-sm space-y-5 rounded-xl border bg-background p-8 shadow-sm"
        style={{
          borderTopColor: status.branding?.primary_color,
          borderTopWidth: 4,
        }}
        onSubmit={submit}
      >
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-3">
            {status.branding?.logo_url && (
              <img
                className="h-10 w-10 object-contain"
                src={status.branding.logo_url}
                alt={`${status.branding.solution_name ?? "Runtime"} logo`}
              />
            )}
            <h1 className="text-2xl font-semibold">
              {status.branding?.solution_name ?? "Unnest Runtime"}
            </h1>
          </div>
          <LanguageButtons
            allowSwitch={status.allow_language_switch !== false}
          />
        </div>
        {status.branding?.login_notice && (
          <p className="rounded border bg-muted p-3 text-sm">
            {status.branding.login_notice}
          </p>
        )}
        <label className="block space-y-1 text-sm">
          <span>사용자 ID / Username</span>
          <input
            className="w-full rounded border bg-background px-3 py-2"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label className="block space-y-1 text-sm">
          <span>비밀번호 / Password</span>
          <input
            className="w-full rounded border bg-background px-3 py-2"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <button
          className="w-full rounded bg-primary px-4 py-3 text-primary-foreground disabled:opacity-50"
          disabled={isPending}
          type="submit"
        >
          {isPending ? "로그인 중… / Signing in…" : "로그인 / Sign in"}
        </button>
        <UnnestMark status={status} />
      </form>
    </main>
  );
}

function RuntimeSetupPage({
  status,
  onComplete,
}: {
  status: SetupStatus;
  onComplete: (value: SetupStatus) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [modelEndpoint, setModelEndpoint] = useState("");
  const [storageEndpoint, setStorageEndpoint] = useState("");
  const [tlsConfigured, setTlsConfigured] = useState(false);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [result, setResult] = useState<SetupResult>();
  const [recoverySaved, setRecoverySaved] = useState(false);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (password !== confirmPassword) {
      setError("비밀번호가 일치하지 않습니다. / Passwords do not match.");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const response = await api.post<SetupResult>("/api/v1/setup", {
        admin_username: username,
        admin_password: password,
        model_endpoint: modelEndpoint || null,
        storage_endpoint: storageEndpoint || null,
        tls_certificate_configured: tlsConfigured,
        secret_values: secrets,
      });
      setResult(response.data);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setSubmitting(false);
    }
  };

  const saveRecoveryIdentity = () => {
    if (!result?.recovery_identity) return;
    const blob = new Blob([`${result.recovery_identity}\n`], {
      type: "text/plain",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `unnest-recovery-${status.release_version ?? "runtime"}.txt`;
    link.click();
    URL.revokeObjectURL(url);
    setRecoverySaved(true);
  };

  if (result?.recovery_identity) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-muted p-6">
        <section className="w-full max-w-xl space-y-5 rounded-xl border bg-background p-8 shadow-sm">
          <LanguageButtons
            allowSwitch={status.allow_language_switch !== false}
          />
          <h1 className="text-2xl font-semibold">
            복구키를 지금 저장하세요 / Save the recovery key now
          </h1>
          <p className="text-sm text-muted-foreground">
            이 키는 다시 표시되지 않으며 암호화된 backup 복원에 필요합니다. The
            private identity is shown only once and is required for restore.
          </p>
          <button
            type="button"
            className="w-full rounded bg-primary px-4 py-3 text-primary-foreground"
            onClick={saveRecoveryIdentity}
          >
            복구키 파일 저장 / Save recovery identity
          </button>
          <button
            type="button"
            className="w-full rounded border px-4 py-3 disabled:opacity-50"
            disabled={!recoverySaved}
            onClick={() => onComplete(result)}
          >
            로그인으로 이동 / Continue to sign in
          </button>
        </section>
      </main>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-muted p-6">
      <form
        className="w-full max-w-2xl space-y-5 rounded-xl border bg-background p-8 shadow-sm"
        style={{
          borderTopColor: status.branding?.primary_color,
          borderTopWidth: 4,
        }}
        onSubmit={submit}
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold">
              {status.branding?.solution_name ?? "Unnest Runtime"} 초기 설정
            </h1>
            <p className="text-sm text-muted-foreground">
              Release {status.release_version ?? "unavailable"}
            </p>
          </div>
          <LanguageButtons
            allowSwitch={status.allow_language_switch !== false}
          />
        </div>
        {!status.license.valid && (
          <p
            role="alert"
            className="rounded bg-destructive/10 p-3 text-sm text-destructive"
          >
            License: {status.license.reason ?? "invalid"}
          </p>
        )}
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="space-y-1 text-sm">
            <span>관리자 ID / Admin username</span>
            <input
              className="w-full rounded border bg-background px-3 py-2"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
              autoComplete="username"
            />
          </label>
          <div />
          <label className="space-y-1 text-sm">
            <span>비밀번호 / Password</span>
            <input
              className="w-full rounded border bg-background px-3 py-2"
              type="password"
              minLength={12}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
              autoComplete="new-password"
            />
          </label>
          <label className="space-y-1 text-sm">
            <span>비밀번호 확인 / Confirm</span>
            <input
              className="w-full rounded border bg-background px-3 py-2"
              type="password"
              minLength={12}
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              required
              autoComplete="new-password"
            />
          </label>
          <label className="space-y-1 text-sm">
            <span>Model endpoint (선택)</span>
            <input
              className="w-full rounded border bg-background px-3 py-2"
              type="url"
              value={modelEndpoint}
              onChange={(event) => setModelEndpoint(event.target.value)}
            />
          </label>
          <label className="space-y-1 text-sm">
            <span>Storage endpoint (선택)</span>
            <input
              className="w-full rounded border bg-background px-3 py-2"
              type="url"
              value={storageEndpoint}
              onChange={(event) => setStorageEndpoint(event.target.value)}
            />
          </label>
          {status.required_secret_names.map((name) => (
            <label key={name} className="space-y-1 text-sm">
              <span>{name}</span>
              <input
                className="w-full rounded border bg-background px-3 py-2"
                type="password"
                value={secrets[name] ?? ""}
                onChange={(event) =>
                  setSecrets((current) => ({
                    ...current,
                    [name]: event.target.value,
                  }))
                }
                required
                autoComplete="off"
              />
            </label>
          ))}
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={tlsConfigured}
            onChange={(event) => setTlsConfigured(event.target.checked)}
          />
          기관 TLS 인증서가 설정됨 / Institution TLS certificate configured
        </label>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <button
          className="w-full rounded bg-primary px-4 py-3 text-primary-foreground disabled:opacity-50"
          disabled={submitting || !status.license.valid}
          type="submit"
        >
          {submitting
            ? "설정 중… / Configuring…"
            : "설정 완료 / Complete setup"}
        </button>
        <UnnestMark status={status} />
      </form>
    </main>
  );
}

function RuntimeChat({ apiVersion }: { apiVersion: string }) {
  const [request, setRequest] = useState('{\n  "message": ""\n}');
  const [response, setResponse] = useState("");
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const run = async () => {
    setRunning(true);
    setError("");
    try {
      const payload = JSON.parse(request);
      const result = await api.post(`/api/${apiVersion}/agent/run`, payload);
      setResponse(JSON.stringify(result.data, null, 2));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setRunning(false);
    }
  };
  return (
    <section className="grid h-full gap-4 lg:grid-cols-2" aria-label="Agent">
      <div className="space-y-3">
        <label className="block text-sm font-medium" htmlFor="runtime-request">
          Agent 요청 JSON / Request JSON
        </label>
        <textarea
          id="runtime-request"
          className="h-80 w-full rounded border bg-background p-3 font-mono text-sm"
          value={request}
          onChange={(event) => setRequest(event.target.value)}
        />
        <button
          type="button"
          className="rounded bg-primary px-5 py-2 text-primary-foreground disabled:opacity-50"
          disabled={running}
          onClick={run}
        >
          {running ? "실행 중… / Running…" : "Agent 실행 / Run agent"}
        </button>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
      </div>
      <div className="space-y-3">
        <h2 className="text-sm font-medium">응답 / Response</h2>
        <pre
          className="h-80 overflow-auto rounded border bg-muted p-3 text-sm"
          aria-live="polite"
        >
          {response}
        </pre>
      </div>
    </section>
  );
}

function RuntimeDocuments({ admin }: { admin: boolean }) {
  const [documents, setDocuments] = useState<RuntimeDocument[]>([]);
  const [duplicateStrategy, setDuplicateStrategy] = useState("skip");
  const [error, setError] = useState("");
  const refresh = async () => {
    try {
      setDocuments((await api.get<RuntimeDocument[]>("/api/v1/files")).data);
    } catch (requestError) {
      setError(errorMessage(requestError));
    }
  };
  useEffect(() => {
    refresh();
  }, []);
  const upload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    form.append("duplicate_strategy", duplicateStrategy);
    try {
      await api.post("/api/v1/files", form);
      await refresh();
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      event.target.value = "";
    }
  };
  const changeDocument = async (
    document: RuntimeDocument,
    action: "delete" | "restore",
  ) => {
    try {
      if (action === "delete") {
        await api.delete(`/api/v1/files/${document.id}`);
      } else {
        await api.post(`/api/v1/files/${document.id}/restore`);
      }
      await refresh();
    } catch (requestError) {
      setError(errorMessage(requestError));
    }
  };
  return (
    <section className="space-y-4" aria-label="Knowledge documents">
      {admin && (
        <div className="flex flex-wrap items-end gap-3 rounded border p-4">
          <label className="space-y-1 text-sm">
            <span>중복 처리 / Duplicate</span>
            <select
              className="block rounded border bg-background px-3 py-2"
              value={duplicateStrategy}
              onChange={(event) => setDuplicateStrategy(event.target.value)}
            >
              <option value="skip">Skip</option>
              <option value="new_version">New version</option>
              <option value="replace">Replace</option>
            </select>
          </label>
          <label className="rounded bg-primary px-4 py-2 text-sm text-primary-foreground">
            파일 업로드 / Upload
            <input className="sr-only" type="file" onChange={upload} />
          </label>
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <div className="overflow-x-auto rounded border">
        <table className="w-full text-left text-sm">
          <thead className="bg-muted">
            <tr>
              <th className="p-3">파일 / File</th>
              <th className="p-3">상태 / Status</th>
              <th className="p-3">Version</th>
              {admin && <th className="p-3">관리 / Actions</th>}
            </tr>
          </thead>
          <tbody>
            {documents.map((document) => (
              <tr key={document.id} className="border-t">
                <td className="p-3">{document.name}</td>
                <td className="p-3">{document.status}</td>
                <td className="p-3">{document.version_number}</td>
                {admin && (
                  <td className="flex gap-2 p-3">
                    <a
                      className="underline"
                      href={`/api/v1/files/${document.id}/download`}
                    >
                      Download
                    </a>
                    <button
                      type="button"
                      className="underline"
                      onClick={() =>
                        changeDocument(
                          document,
                          document.status === "trash" ? "restore" : "delete",
                        )
                      }
                    >
                      {document.status === "trash" ? "Restore" : "Delete"}
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function RuntimeAdmin() {
  const [backups, setBackups] = useState<RuntimeBackup[]>([]);
  const [users, setUsers] = useState<RuntimeManagedUser[]>([]);
  const [license, setLicense] = useState<Record<string, unknown>>({});
  const [audit, setAudit] = useState<{ integrity?: Record<string, unknown> }>(
    {},
  );
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newRole, setNewRole] = useState<"admin" | "general">("general");
  const [apiKeyName, setApiKeyName] = useState("");
  const [issuedApiKey, setIssuedApiKey] = useState("");
  const refresh = async () => {
    try {
      const [backupResponse, licenseResponse, auditResponse, userResponse] =
        await Promise.all([
          api.get<RuntimeBackup[]>("/api/v1/admin/backups"),
          api.get<Record<string, unknown>>("/api/v1/admin/license"),
          api.get("/api/v1/admin/audit?limit=20"),
          api.get<RuntimeManagedUser[]>("/api/v1/admin/users"),
        ]);
      setBackups(backupResponse.data);
      setLicense(licenseResponse.data);
      setAudit(auditResponse.data);
      setUsers(userResponse.data);
    } catch (requestError) {
      setError(errorMessage(requestError));
    }
  };
  useEffect(() => {
    refresh();
  }, []);
  const createBackup = async () => {
    setCreating(true);
    try {
      await api.post("/api/v1/admin/backups");
      await refresh();
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setCreating(false);
    }
  };
  const createUser = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    try {
      await api.post("/api/v1/admin/users", {
        username: newUsername,
        password: newPassword,
        role: newRole,
        is_active: true,
      });
      setNewUsername("");
      setNewPassword("");
      await refresh();
    } catch (requestError) {
      setError(errorMessage(requestError));
    }
  };
  const createApiKey = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    try {
      const response = await api.post<{ api_key: string }>(
        "/api/v1/admin/api-keys",
        {
          name: apiKeyName,
          rate_limit_per_minute: 60,
          max_concurrent_runs: 4,
          max_request_bytes: 10485760,
          daily_quota: 10000,
        },
      );
      setIssuedApiKey(response.data.api_key);
      setApiKeyName("");
    } catch (requestError) {
      setError(errorMessage(requestError));
    }
  };
  return (
    <section className="grid gap-5 lg:grid-cols-2" aria-label="Operations">
      <div className="space-y-3 rounded border p-5">
        <h2 className="font-semibold">License</h2>
        <pre className="overflow-auto rounded bg-muted p-3 text-xs">
          {JSON.stringify(license, null, 2)}
        </pre>
      </div>
      <div className="space-y-3 rounded border p-5">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">Backup</h2>
          <button
            type="button"
            className="rounded bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-50"
            disabled={creating}
            onClick={createBackup}
          >
            {creating ? "Creating…" : "Create backup"}
          </button>
        </div>
        <ul className="space-y-2 text-sm">
          {backups.map((backup) => (
            <li key={backup.id} className="rounded bg-muted p-3">
              <a
                className="underline"
                href={`/api/v1/admin/backups/${backup.id}/download`}
              >
                {new Date(backup.created_at).toLocaleString()}
              </a>
              <span className="ml-2 text-muted-foreground">
                {(backup.size_bytes / 1024 / 1024).toFixed(1)} MB
              </span>
            </li>
          ))}
        </ul>
      </div>
      <div className="space-y-3 rounded border p-5">
        <h2 className="font-semibold">Users</h2>
        <ul className="space-y-1 text-sm">
          {users.map((user) => (
            <li
              key={user.id}
              className="flex justify-between rounded bg-muted p-2"
            >
              <span>{user.username}</span>
              <span>
                {user.role} · {user.is_active ? "active" : "disabled"}
              </span>
            </li>
          ))}
        </ul>
        <form className="grid gap-2 sm:grid-cols-3" onSubmit={createUser}>
          <input
            className="rounded border bg-background px-3 py-2 text-sm"
            aria-label="New username"
            placeholder="Username"
            value={newUsername}
            onChange={(event) => setNewUsername(event.target.value)}
            required
          />
          <input
            className="rounded border bg-background px-3 py-2 text-sm"
            aria-label="New user password"
            placeholder="Password"
            type="password"
            minLength={12}
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
            required
          />
          <select
            className="rounded border bg-background px-3 py-2 text-sm"
            aria-label="New user role"
            value={newRole}
            onChange={(event) =>
              setNewRole(event.target.value as "admin" | "general")
            }
          >
            <option value="general">General</option>
            <option value="admin">Admin</option>
          </select>
          <button
            className="rounded border px-3 py-2 text-sm sm:col-span-3"
            type="submit"
          >
            Create user
          </button>
        </form>
      </div>
      <div className="space-y-3 rounded border p-5">
        <h2 className="font-semibold">API keys</h2>
        <form className="flex gap-2" onSubmit={createApiKey}>
          <input
            className="min-w-0 flex-1 rounded border bg-background px-3 py-2 text-sm"
            aria-label="API key name"
            placeholder="Integration name"
            value={apiKeyName}
            onChange={(event) => setApiKeyName(event.target.value)}
            required
          />
          <button className="rounded border px-3 py-2 text-sm" type="submit">
            Issue key
          </button>
        </form>
        {issuedApiKey && (
          <div className="rounded border bg-muted p-3 text-sm text-foreground">
            <p>Copy now. This key is shown once.</p>
            <code className="break-all">{issuedApiKey}</code>
          </div>
        )}
      </div>
      <div className="space-y-3 rounded border p-5 lg:col-span-2">
        <h2 className="font-semibold">Audit chain</h2>
        <pre className="overflow-auto rounded bg-muted p-3 text-xs">
          {JSON.stringify(audit.integrity ?? {}, null, 2)}
        </pre>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </section>
  );
}

function RuntimeConsole({
  user,
  status,
  onLogout,
}: {
  user: RuntimeUser;
  status: SetupStatus;
  onLogout: () => Promise<void>;
}) {
  const [tab, setTab] = useState<RuntimeTab>("agent");
  const admin = Boolean(user.is_superuser);
  const apiVersion = useMemo(() => status.api_versions?.[0] ?? "v1", [status]);
  const tabs: Array<[RuntimeTab, string]> = [
    ["agent", "Agent"],
    ["documents", "Knowledge"],
  ];
  if (admin) tabs.push(["operations", "Operations"]);
  return (
    <div className="flex min-h-screen flex-col bg-background">
      <header
        className="flex flex-wrap items-center justify-between gap-4 border-b border-t-4 px-6 py-4"
        style={{ borderTopColor: status.branding?.primary_color }}
      >
        <div>
          <h1 className="text-xl font-semibold">
            {status.branding?.solution_name ?? "Unnest Runtime"}
          </h1>
          <p className="text-xs text-muted-foreground">
            {status.branding?.organization_name &&
              `${status.branding.organization_name} · `}
            Release {status.release_version} · API {apiVersion}
          </p>
          <UnnestMark status={status} />
        </div>
        <nav className="flex gap-1" aria-label="Runtime">
          {tabs.map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={`rounded px-4 py-2 text-sm ${tab === value ? "bg-muted font-medium" : ""}`}
              aria-current={tab === value ? "page" : undefined}
              onClick={() => setTab(value)}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="flex items-center gap-3">
          <LanguageButtons
            allowSwitch={status.allow_language_switch !== false}
          />
          <span className="text-sm">{user.username}</span>
          <button
            type="button"
            className="rounded border px-3 py-2 text-sm"
            onClick={onLogout}
          >
            Logout
          </button>
        </div>
      </header>
      <main className="flex-1 p-6">
        {tab === "agent" && <RuntimeChat apiVersion={apiVersion} />}
        {tab === "documents" && <RuntimeDocuments admin={admin} />}
        {tab === "operations" && admin && <RuntimeAdmin />}
      </main>
    </div>
  );
}

export default function RuntimePage() {
  const [status, setStatus] = useState<SetupStatus>();
  const [user, setUser] = useState<RuntimeUser | null>();
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .get<SetupStatus>("/api/v1/setup/status")
      .then((response) => setStatus(response.data))
      .catch((requestError) => setError(errorMessage(requestError)));
  }, []);
  useEffect(() => {
    if (
      status?.default_language &&
      !localStorage.getItem("languagePreference")
    ) {
      loadLanguage(status.default_language).then(() => {
        document.documentElement.lang = status.default_language ?? "ko";
        return i18n.changeLanguage(status.default_language);
      });
    }
  }, [status?.default_language]);
  useEffect(() => {
    if (!status?.complete) return;
    api
      .get<{ authenticated: boolean; user?: RuntimeUser }>("/api/v1/session")
      .then((response) =>
        setUser(
          response.data.authenticated && response.data.user
            ? response.data.user
            : null,
        ),
      )
      .catch(() => setUser(null));
  }, [status?.complete]);

  if (error) {
    return (
      <main className="p-8 text-destructive" role="alert">
        {error}
      </main>
    );
  }
  if (!status) {
    return (
      <main className="flex min-h-screen items-center justify-center">
        Loading…
      </main>
    );
  }
  if (!status.complete) {
    return <RuntimeSetupPage status={status} onComplete={setStatus} />;
  }
  if (user === undefined) {
    return (
      <main className="flex min-h-screen items-center justify-center">
        Loading…
      </main>
    );
  }
  if (user === null) {
    return <RuntimeLogin status={status} onLogin={setUser} />;
  }
  return (
    <RuntimeConsole
      user={user}
      status={status}
      onLogout={async () => {
        try {
          await api.post("/api/v1/logout");
        } finally {
          setUser(null);
        }
      }}
    />
  );
}
