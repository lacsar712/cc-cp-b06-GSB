import { useCallback, useEffect, useState } from "preact/hooks";

const TOKEN_KEY = "coldchain_token";
const USER_KEY = "coldchain_user";

function verdictClass(v, status) {
  if (v === "合格") return "tag pass";
  if (v === "超温") return "tag fail";
  if (status === "pending" || status === "processing") return "tag wait";
  return "tag wait";
}

function displayVerdict(row) {
  if (row.verdict) return row.verdict;
  if (row.status === "pending") return "待处理";
  if (row.status === "processing") return "处理中";
  return "—";
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(
    d.getHours()
  )}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function actionLabel(action) {
  return action === "retire" ? "退役封存" : "恢复在役";
}

export function App() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY));
  const [user, setUser] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem(USER_KEY) || "null");
    } catch {
      return null;
    }
  });
  const [view, setView] = useState("desk");
  const [loginForm, setLoginForm] = useState({ username: "logger", password: "log123456" });
  const [submitForm, setSubmitForm] = useState({ probe_id: "", temp_c: "" });
  const [rows, setRows] = useState([]);
  const [probes, setProbes] = useState([]);
  const [events, setEvents] = useState([]);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(false);
  const [busyProbe, setBusyProbe] = useState("");

  const authHeaders = useCallback(() => {
    const h = { "Content-Type": "application/json" };
    if (token) h.Authorization = `Bearer ${token}`;
    return h;
  }, [token]);

  const loadReadings = useCallback(async () => {
    if (!token) return;
    const res = await fetch("/api/readings", { headers: authHeaders() });
    if (!res.ok) {
      setError("加载列表失败，请重新登录");
      return;
    }
    setRows(await res.json());
  }, [token, authHeaders]);

  const loadProbeData = useCallback(async () => {
    if (!token) return;
    const [pr, ev] = await Promise.all([
      fetch("/api/probes", { headers: authHeaders() }),
      fetch("/api/probe-events", { headers: authHeaders() }),
    ]);
    if (!pr.ok || !ev.ok) {
      setError("加载退役信息失败，请重新登录");
      return;
    }
    setProbes(await pr.json());
    setEvents(await ev.json());
  }, [token, authHeaders]);

  useEffect(() => {
    if (!token) {
      setRows([]);
      setProbes([]);
      setEvents([]);
      return undefined;
    }
    if (view === "desk") {
      loadReadings();
      const t = setInterval(loadReadings, 3000);
      return () => clearInterval(t);
    }
    loadProbeData();
    const t = setInterval(loadProbeData, 3000);
    return () => clearInterval(t);
  }, [loadReadings, loadProbeData, token, view]);

  async function onLogin(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(loginForm),
      });
      if (!res.ok) {
        setError("用户名或密码错误");
        return;
      }
      const data = await res.json();
      localStorage.setItem(TOKEN_KEY, data.access_token);
      localStorage.setItem(
        USER_KEY,
        JSON.stringify({ username: data.username, role: data.role })
      );
      setToken(data.access_token);
      setUser({ username: data.username, role: data.role });
    } finally {
      setLoading(false);
    }
  }

  function logout() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    setToken(null);
    setUser(null);
    setRows([]);
    setProbes([]);
    setEvents([]);
    setView("desk");
  }

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    setMsg("");
    setLoading(true);
    try {
      const res = await fetch("/api/readings", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
          probe_id: submitForm.probe_id,
          temp_c: parseFloat(submitForm.temp_c),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(data.detail || "提交失败");
        return;
      }
      setMsg(data.message || "已提交");
      setSubmitForm({ probe_id: "", temp_c: "" });
      await loadReadings();
    } finally {
      setLoading(false);
    }
  }

  async function onChangeState(probeId, action) {
    setError("");
    setMsg("");
    let note = "";
    if (action === "retire") {
      note = window.prompt(`将探头 ${probeId} 退役封存？可填写备注（留空即可）`, "");
      if (note === null) return;
      note = note.trim();
    } else if (!window.confirm(`确认恢复探头 ${probeId} 为在役？`)) {
      return;
    }
    setBusyProbe(`${action}:${probeId}`);
    try {
      const res = await fetch(
        action === "retire" ? "/api/probes/retire" : "/api/probes/restore",
        {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({ probe_id: probeId, note }),
        }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(data.detail || "操作失败");
        return;
      }
      setMsg(
        action === "retire"
          ? `探头 ${probeId} 已退役封存`
          : `探头 ${probeId} 已恢复在役`
      );
      await loadProbeData();
    } finally {
      setBusyProbe("");
    }
  }

  if (!token) {
    return (
      <div class="wrap">
        <h1>冷链探头超温台</h1>
        <p class="sub">记录员提交探头编号与摄氏温度，后台工人认领后判定合格或超温。</p>
        <div class="card">
          <form onSubmit={onLogin}>
            <div class="row">
              <label>
                用户名
                <input
                  value={loginForm.username}
                  onInput={(e) =>
                    setLoginForm({ ...loginForm, username: e.target.value })
                  }
                />
              </label>
              <label>
                密码
                <input
                  type="password"
                  value={loginForm.password}
                  onInput={(e) =>
                    setLoginForm({ ...loginForm, password: e.target.value })
                  }
                />
              </label>
              <button type="submit" disabled={loading}>
                登录
              </button>
            </div>
            {error && <p class="err">{error}</p>}
          </form>
          <p class="sub" style={{ marginBottom: 0 }}>
            记录员 logger / log123456 · 值班员 watcher / watch123456
          </p>
        </div>
      </div>
    );
  }

  const isWriter = user?.role === "writer";
  const activeProbes = probes.filter((p) => p.state === "active");
  const retiredProbes = probes.filter((p) => p.state === "retired");

  return (
    <div class="wrap">
      <div class="topbar">
        <div>
          <h1>冷链探头超温台</h1>
          <p class="sub">温度不超过 8℃ 为合格，否则为超温。</p>
        </div>
        <div class="user">
          {user?.username}（{isWriter ? "记录员" : "值班员"}）
          <button type="button" class="secondary" style={{ marginLeft: "0.5rem" }} onClick={logout}>
            退出
          </button>
        </div>
      </div>

      <nav class="tabs">
        <button
          type="button"
          class={view === "desk" ? "tab active" : "tab"}
          onClick={() => {
            setView("desk");
            setError("");
            setMsg("");
          }}
        >
          读数台
        </button>
        <button
          type="button"
          class={view === "retire" ? "tab active" : "tab"}
          onClick={() => {
            setView("retire");
            setError("");
            setMsg("");
          }}
        >
          探头退役
        </button>
      </nav>

      {view === "desk" && (
        <>
          {isWriter && (
            <div class="card">
              <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>提交读数</h2>
              <form onSubmit={onSubmit}>
                <div class="row">
                  <label>
                    探头编号
                    <input
                      required
                      value={submitForm.probe_id}
                      onInput={(e) =>
                        setSubmitForm({ ...submitForm, probe_id: e.target.value })
                      }
                      placeholder="例如 探头C03"
                    />
                  </label>
                  <label>
                    温度（℃）
                    <input
                      required
                      type="number"
                      step="0.1"
                      value={submitForm.temp_c}
                      onInput={(e) =>
                        setSubmitForm({ ...submitForm, temp_c: e.target.value })
                      }
                    />
                  </label>
                  <button type="submit" disabled={loading}>
                    提交
                  </button>
                </div>
                {error && <p class="err">{error}</p>}
                {msg && <p class="ok">{msg}</p>}
              </form>
            </div>
          )}

          <div class="card">
            <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>读数列表</h2>
            <table>
              <thead>
                <tr>
                  <th>编号</th>
                  <th>探头</th>
                  <th>温度℃</th>
                  <th>结论</th>
                  <th>说明</th>
                  <th>状态</th>
                  <th>提交人</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td>{r.id}</td>
                    <td>{r.probe_id}</td>
                    <td>{r.temp_c}</td>
                    <td>
                      <span class={verdictClass(r.verdict, r.status)}>
                        {displayVerdict(r)}
                      </span>
                    </td>
                    <td>{r.reason || "—"}</td>
                    <td>{r.status}</td>
                    <td>{r.created_by}</td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td colspan="7">暂无数据</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      {view === "retire" && (
        <>
          {error && (
            <div class="card">
              <p class="err" style={{ margin: 0 }}>
                {error}
              </p>
            </div>
          )}
          {msg && (
            <div class="card">
              <p class="ok" style={{ margin: 0 }}>
                {msg}
              </p>
            </div>
          )}

          <div class="card">
            <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>在役探头</h2>
            <table>
              <thead>
                <tr>
                  <th>探头代号</th>
                  <th>状态</th>
                  <th>最近操作人</th>
                  <th>更新时间</th>
                  {isWriter && <th>操作</th>}
                </tr>
              </thead>
              <tbody>
                {activeProbes.map((p) => (
                  <tr key={p.probe_id}>
                    <td>{p.probe_id}</td>
                    <td>
                      <span class="tag pass">在役</span>
                    </td>
                    <td>{p.updated_by || "—"}</td>
                    <td>{fmtTime(p.updated_at)}</td>
                    {isWriter && (
                      <td>
                        <button
                          type="button"
                          class="danger"
                          disabled={busyProbe === `retire:${p.probe_id}`}
                          onClick={() => onChangeState(p.probe_id, "retire")}
                        >
                          退役
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
                {activeProbes.length === 0 && (
                  <tr>
                    <td colspan={isWriter ? 5 : 4}>暂无在役探头</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div class="card">
            <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>退役探头（已封存）</h2>
            <table>
              <thead>
                <tr>
                  <th>探头代号</th>
                  <th>状态</th>
                  <th>封存操作人</th>
                  <th>封存时间</th>
                  {isWriter && <th>操作</th>}
                </tr>
              </thead>
              <tbody>
                {retiredProbes.map((p) => (
                  <tr key={p.probe_id}>
                    <td>{p.probe_id}</td>
                    <td>
                      <span class="tag retired">退役</span>
                    </td>
                    <td>{p.updated_by || "—"}</td>
                    <td>{fmtTime(p.updated_at)}</td>
                    {isWriter && (
                      <td>
                        <button
                          type="button"
                          disabled={busyProbe === `restore:${p.probe_id}`}
                          onClick={() => onChangeState(p.probe_id, "restore")}
                        >
                          恢复在役
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
                {retiredProbes.length === 0 && (
                  <tr>
                    <td colspan={isWriter ? 5 : 4}>暂无退役探头</td>
                  </tr>
                )}
              </tbody>
            </table>
            <p class="sub" style={{ margin: "0.75rem 0 0", fontSize: "0.8rem" }}>
              退役封存后的探头禁止再提交新温度，恢复在役后方可继续提交。
            </p>
          </div>

          <div class="card">
            <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>退役流水</h2>
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>探头代号</th>
                  <th>动作</th>
                  <th>操作人</th>
                  <th>备注</th>
                </tr>
              </thead>
              <tbody>
                {events.map((ev) => (
                  <tr key={ev.id}>
                    <td>{fmtTime(ev.created_at)}</td>
                    <td>{ev.probe_id}</td>
                    <td>
                      <span class={ev.action === "retire" ? "tag retired" : "tag pass"}>
                        {actionLabel(ev.action)}
                      </span>
                    </td>
                    <td>{ev.operator}</td>
                    <td>{ev.note || "—"}</td>
                  </tr>
                ))}
                {events.length === 0 && (
                  <tr>
                    <td colspan="5">暂无退役流水</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {!isWriter && (
            <p class="sub" style={{ fontSize: "0.8rem" }}>
              当前为值班员（观察岗）账号，仅可查看，不能退役、恢复或提交读数。
            </p>
          )}
        </>
      )}
    </div>
  );
}
