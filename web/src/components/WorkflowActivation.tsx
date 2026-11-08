import { useCallback, useEffect, useState } from 'react';
import {
  acceptGpuWorkflowSmoke, apiMessage, getGpuComputeSpecs, getGoogleAccounts, getProjects, getWorkflowActivation,
  publishGpuWorkflow, realSmokeGpuWorkflow, reviewGpuModel, reviewGpuWorkflow, validateGpuWorkflow,
  type ComputeSpec, type GpuWorkflow, type WorkflowActivation, type GoogleAccount,
} from '../lib/gpu-api';
import { isColabAccountQueryable, resolveGpuCheckRequest, selectEligibleColabGpuSpec } from '../lib/colab-check';
import styles from '../gpu.module.css';

const STEPS_POLL_MS = 6000;
const POLL_LIMIT = 60;

const sleep = (ms: number) => new Promise((resolve) => { window.setTimeout(resolve, ms); });

export function WorkflowActivationPanel({
  workflow, onClose, onDone,
}: {
  workflow: GpuWorkflow;
  onClose: () => void;
  onDone: () => void;
}) {
  const [data, setData] = useState<WorkflowActivation | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [consent, setConsent] = useState(false);
  const [projectId, setProjectId] = useState('');
  const [accountId, setAccountId] = useState('');
  const [accountList, setAccountList] = useState<GoogleAccount[]>([]);
  const [spec, setSpec] = useState<ComputeSpec | null>(null);
  const [specsLoaded, setSpecsLoaded] = useState(false);

  const say = (line: string) => setLog((curr) => [...curr, line]);

  const refresh = useCallback(async () => {
    const next = await getWorkflowActivation(workflow.id);
    setData(next);
    return next;
  }, [workflow.id]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const snapshot = await refresh();
        const [, accounts] = await Promise.all([getProjects().then((list) => {
          if (alive) setProjectId(list[0]?.id || '');
        }), getGoogleAccounts()]);
        if (!alive) return;
        setAccountList(accounts);
        const request = resolveGpuCheckRequest(
          { real_enabled: snapshot.real_enabled, offline_mode: false },
          accounts,
          null,
        );
        if (request.provider !== 'colab') {
          throw new Error('没有已启用且已连接 Colab 的账号，无法读取真实 GPU 规格');
        }
        const eligible = selectEligibleColabGpuSpec(await getGpuComputeSpecs('colab', request.accountId));
        if (!eligible) throw new Error('当前账号没有可用的真实 GPU 规格（已过滤 CPU/TPU）');
        if (!alive) return;
        // Keep the identity and the complete structured spec together.  The
        // smoke request below must use the same account that produced this spec.
        setAccountId(request.accountId);
        setSpec(eligible);
        setSpecsLoaded(true);
      } catch (err) {
        if (alive) {
          setError(apiMessage(err));
          setSpecsLoaded(true);
        }
      }
    };
    void load();
    return () => { alive = false; };
  }, [refresh]);

  const onSelectAccount = async (newAccountId: string) => {
    setAccountId(newAccountId);
    setSpecsLoaded(false);
    setError('');
    try {
      const eligible = selectEligibleColabGpuSpec(await getGpuComputeSpecs('colab', newAccountId));
      if (!eligible) throw new Error('所选账号没有可用的真实 GPU 规格（已过滤 CPU/TPU）');
      setSpec(eligible);
    } catch (err) {
      setError(apiMessage(err));
    } finally {
      setSpecsLoaded(true);
    }
  };

  const stepDone = (key: string) => data?.steps.find((step) => step.key === key)?.done === true;

  async function waitForSmoke(): Promise<WorkflowActivation> {
    for (let i = 0; i < POLL_LIMIT; i += 1) {
      const snapshot = await refresh();
      const state = snapshot.smoke?.state;
      if (state && ['completed', 'failed', 'cancelled'].includes(state)) {
        say(state === 'completed' ? '冒烟任务已完成' : `冒烟任务结束：${state}`);
        return snapshot;
      }
      say(`等待 GPU 冒烟…（${i + 1}）`);
      await sleep(STEPS_POLL_MS);
    }
    throw new Error('冒烟超时，请稍后回来查看任务状态');
  }

  async function runAll() {
    if (busy) return;
    setError('');
    setBusy(true);
    try {
      let snapshot = await refresh();
      if (snapshot.blockers.length) throw new Error(snapshot.blockers[0]);
      if (!stepDone('validate')) {
        say('正在校验模板…');
        await validateGpuWorkflow(workflow.id);
        snapshot = await refresh();
      }
      if (!stepDone('model_review')) {
        if (!snapshot.model) throw new Error('找不到绑定的模型记录');
        say('正在记录模型来源与许可…');
        await reviewGpuModel(snapshot.model.model_id);
        snapshot = await refresh();
      }
      if (!stepDone('workflow_review')) {
        say('正在锁定权重与节点…');
        await reviewGpuWorkflow(workflow.id);
        snapshot = await refresh();
      }

      // 检查是否有已经跑通的冒烟任务，优先复用，杜绝重复开机扣费
      const hasCompletedSmoke = Boolean(
        snapshot.smoke_job_completed ||
        (snapshot.smoke?.state === 'completed' && snapshot.smoke?.job_id)
      );

      if (!stepDone('real_smoke')) {
        if (hasCompletedSmoke) {
          say('检测到已有完成的冒烟任务，直接进入验收…');
        } else {
          if (!projectId) throw new Error('请先创建一个项目，冒烟任务要落在项目里');
          if (!accountId || !spec) throw new Error('当前账号没有可用的 GPU 规格，请先授权 Colab');
          say('正在提交真实 GPU 冒烟…');
          await realSmokeGpuWorkflow(
            workflow.id,
            {
              confirmed: true,
              project_id: projectId,
              params: {},
              asset_ids: [],
              account_id: accountId,
              // Pass the exact structured key returned by the same account's
              // read-only discovery call; never coerce it to a string.
              spec,
            },
            `smoke-${workflow.id}-${Date.now()}`,
          );
          say('已进入队列，最长 5 分钟');
          snapshot = await waitForSmoke();
        }
      }
      if (!snapshot.smoke_job_completed && snapshot.smoke?.state !== 'completed') {
        throw new Error(snapshot.smoke?.error_message || '冒烟未通过，详见任务详情');
      }
      if (!stepDone('accept_smoke')) {
        const jobId = snapshot.smoke?.job_id;
        if (!jobId) throw new Error('找不到可验收的冒烟任务');
        say('正在验收冒烟输出…');
        await acceptGpuWorkflowSmoke(workflow.id, jobId);
        snapshot = await refresh();
      }
      if (!snapshot.done) {
        say('正在发布并设为当前版本…');
        await publishGpuWorkflow(workflow.id);
        snapshot = await refresh();
      }
      say('完成，可以回创作页用了');
      onDone();
    } catch (err) {
      setError(apiMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.activationPanel}>
      <p className="muted small">
        这一步会真实分配 GPU 跑一次冒烟（最长 5 分钟，按分钟计费）。通过后自动发布并设为当前版本。
      </p>
      {data?.blockers.length ? (
        <div className={styles.notice}>
          {data.blockers.map((item) => <p key={item} className="small">{item}</p>)}
        </div>
      ) : null}
      <ol className={styles.activationSteps}>
        {(data?.steps || []).map((step) => (
          <li key={step.key} data-state={step.done ? 'done' : step.failed ? 'failed' : step.running ? 'running' : step.next ? 'next' : 'todo'}>
            <span>{step.label}</span>
            <span className="pill" data-state={step.done ? 'active' : undefined}>
              {step.done ? '已完成' : step.failed ? '失败' : step.running ? '进行中' : step.next ? '待执行' : '待执行'}
            </span>
          </li>
        ))}
      </ol>
      {error ? <p className={styles.errorText}>{error}</p> : null}
      {log.length ? (
        <details className="foldable" open>
          <summary>执行记录</summary>
          <div className="foldContent">
            {log.map((line, index) => <p key={`${line}-${index}`} className="muted small">{line}</p>)}
          </div>
        </details>
      ) : null}

      {accountList.length > 1 && (
        <div style={{ marginBottom: '12px' }}>
          <label className="small muted" style={{ display: 'block', marginBottom: '4px' }}>
            冒烟测试执行账号：
          </label>
          <select
            className="select"
            value={accountId}
            disabled={busy || data?.done}
            onChange={(e) => void onSelectAccount(e.target.value)}
          >
            {accountList.filter(isColabAccountQueryable).map((acc) => (
              <option key={acc.account_id} value={acc.account_id}>
                {acc.alias || acc.account_id} {acc.email_masked ? `(${acc.email_masked})` : ''}
              </option>
            ))}
          </select>
        </div>
      )}

      <label className={styles.checkline}>
        <input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />
        <span>我知道会真实开 GPU 并产生费用</span>
      </label>
      <div className="actions">
        <button className="button" type="button" disabled={!consent || busy || !specsLoaded} onClick={() => void runAll()}>
          {busy ? '执行中…' : data?.done ? '重新检查' : '一键跑通并启用'}
        </button>
        <button className="button secondary" type="button" disabled={busy} onClick={onClose}>关闭</button>
      </div>
    </div>
  );
}
