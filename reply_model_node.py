from __future__ import annotations

from typing import Any

from .pipeline_models import QQModelResult, QQPipelineStageTrace, QQReplyContext


class QQReplyModelNode:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    async def generate(self, context: QQReplyContext) -> QQModelResult:
        if not self._should_generate(context):
            return QQModelResult(
                reply_text=None,
                source="none",
                traces=[
                    QQPipelineStageTrace(
                        stage="model_primary",
                        status="skipped",
                        metadata={"reason": "permission_blocked"},
                    )
                ],
            )

        deepchat = getattr(self.plugin, "deepchat_service", None)
        if deepchat is not None and deepchat.generation_mode == "direct":
            return await self._run_direct_primary(context)

        primary_result = await self._run_primary(context)
        if not primary_result.allow_fallback:
            return primary_result

        fallback_reply = await self._run_fallback(context)
        primary_result.traces.append(
            QQPipelineStageTrace(
                stage="model_fallback",
                status="success" if fallback_reply else "empty",
                metadata={
                    "reply_length": len(fallback_reply or ""),
                },
            )
        )
        if fallback_reply:
            # 生产管线的 fallback 成功路径：与 legacy generate_from_context
            # 对偶，跑同一份 scoped 记忆钩子（成员 bucket / mention 计数）。
            await self.plugin.reply_generation_service.run_fallback_memory_hooks(
                context, fallback_reply,
            )
            return QQModelResult(
                reply_text=fallback_reply,
                source="direct_llm_fallback",
                used_fallback=True,
                traces=primary_result.traces,
            )
        return QQModelResult(reply_text=None, source="none", used_fallback=True, traces=primary_result.traces)

    async def _run_direct_primary(self, context: QQReplyContext) -> QQModelResult:
        """Deep-chat direct mode: per-turn prompt, no host-session freeze.

        The host session freezes its instructions for the session's lifetime,
        so mood/relationship fragments injected into `system_prompt` only
        reach the model when a new session is created. Direct mode uses the
        existing raw-LLM path, which reads `context.system_prompt` every turn,
        at the cost of losing host-session tools/memory plumbing.
        """
        fallback_reply = await self._run_fallback(context)
        trace = QQPipelineStageTrace(
            stage="model_direct",
            status="success" if fallback_reply else "empty",
            metadata={"reply_length": len(fallback_reply or "")},
        )
        if fallback_reply:
            await self.plugin.reply_generation_service.run_fallback_memory_hooks(
                context, fallback_reply,
            )
            return QQModelResult(
                reply_text=fallback_reply,
                source="deepchat_direct",
                used_fallback=True,
                traces=[trace],
            )
        return QQModelResult(reply_text=None, source="none", used_fallback=True, traces=[trace])

    def _should_generate(self, context: QQReplyContext) -> bool:
        # 开放平台：全部生成（不限制权限）
        if self.plugin.qq_client and not self.plugin.qq_client.needs_attention:
            return True
        return context.is_group or context.permission_level in ["admin", "trusted"]

    async def _run_primary(self, context: QQReplyContext) -> QQModelResult:
        return await self.plugin.reply_generation_service.run_primary_session_call(context)

    async def _run_fallback(self, context: QQReplyContext) -> str | None:
        return await self.plugin.reply_generation_service.generate_fallback_from_context(context)
