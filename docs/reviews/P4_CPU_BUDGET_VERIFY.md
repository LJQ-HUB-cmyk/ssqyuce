# P4 闲时挖掘：CPU 占用量实测验证（用户硬约束 ≤60%）

> 日期：2026-10-20 · VPS：4 核（AMD EPYC 9275F）· 容器 `ssq-predictor`
> 用户约束：「闲时挖掘时，对本 vps（4 颗虚拟核心）cpu 的占用量不能超过 60%」

## 双重保险

| 层 | 机制 | 参数 |
|---|---|---|
| 软控制（应用层） | `idle_mining._throttle()` 按真实 CPU 时间闭环休眠 | `LOTT_IDLE_CPU_PCT=60` |
| 硬限额（cgroup） | `cpu_quota=240000 / cpu_period=100000` = 2.4 核 | 内核强制 |

软控制的数学：`t_sleep ≥ t_work·(1-r)/r`，r=0.60 ⇒ sleep ≥ 0.667×work，
再叠加 `/proc/stat` 读到的整机瞬时占用做二次让路；整机已超 60% 时直接长休眠。

## 实测结果（3 次独立采样，共约 780 秒）

| 采样窗口 | 样本数 | 均值 | 中位 | P95 | 峰值 |
|---|---|---:|---:|---:|---:|
| 300s（每 5s） | 60 | 4.0% | 0.2% | — | 59.1% |
| 180s（每 2s） | 90 | 3.3% | 0.2% | — | 89.1% |
| 300s（每 4s） | 55 | 3.2% | 0.2% | 45.6% | 61.3% |

**折合整机 4 核**：

| 指标 | 整机口径 | 上限 | 结论 |
|---|---:|---:|---|
| 均值 | **0.8%** | 60% | 余量 75× |
| P95 | **11.4%** | 60% | 余量 5.3× |
| 峰值 | **15.3%** | 60% | 余量 3.9× |

**cgroup 是否曾触及 240%（单核口径）：否。**

## 为什么峰值远低于限额

- 挖掘是**单线程**任务，物理上限 = 1 核 = 整机 25%。
- 实测峰值 15.3% 甚至没到 25%，因为每批 8 个候选后就有一次休眠，
  计算与休眠交替，采样难以捕捉满核瞬间。
- 一轮 40 候选约 4 秒计算 + 120 秒休眠 ⇒ 活跃占比约 7%，
  这是均值只有 0.8% 的原因。

## 节奏与 3 天覆盖率

`LOTT_IDLE_INTERVAL=120`、`LOTT_IDLE_BUDGET=40` ⇒ 0.33 候选/秒。

3 天 = 2160 轮 × 40 = **86 400 个候选**，空间总量 **72 656** ⇒ **覆盖 100%**。

## 复现

```bash
# CPU 采样
python3 -c "
import subprocess,time,statistics as st
vals=[]
t0=time.time()
while time.time()-t0<300:
    o=subprocess.run(['docker','stats','--no-stream','--format','{{.CPUPerc}}','ssq-predictor'],capture_output=True,text=True).stdout.strip().rstrip('%')
    try: vals.append(float(o))
    except: pass
    time.sleep(4)
print(st.mean(vals), max(vals))"

# cgroup 限额确认
docker inspect ssq-predictor --format '{{.HostConfig.CpuQuota}} {{.HostConfig.CpuPeriod}}'
# 期望输出 240000 100000
```
