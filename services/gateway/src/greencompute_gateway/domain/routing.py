from greencompute_protocol import (
    ChatCompletionChoice,
    ChatCompletionMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    DeploymentRecord,
)


class NoReadyDeploymentError(RuntimeError):
    """No deployment can take this request. As such (not one of the kinds
    below): the model has one starting, so it is worth retrying shortly
    (409 model_not_ready)."""


class ModelNotFoundError(NoReadyDeploymentError):
    """No model by that name, id or address that this caller may use
    (404 model_not_found). Someone else's private model is answered the
    same way, so its existence can't be probed."""


class ModelNotRunningError(NoReadyDeploymentError):
    """A private model with nothing running or starting: paused after
    sitting idle, or its last deployment ended. Retrying won't help until
    its owner resumes or redeploys it (409 model_not_running)."""

    def __init__(self, message: str, *, deployment_id: str | None = None, paused: bool = False) -> None:
        super().__init__(message)
        self.deployment_id = deployment_id
        self.paused = paused


class ModelUnavailableError(NoReadyDeploymentError):
    """The model exists but no replica can serve right now: its health
    check fails, its machine stopped reporting, or (a shared model) no
    replica is up yet. The platform brings one back; retry after
    Retry-After (503 model_unavailable)."""


class InferenceRouter:
    def render_chat_response(
        self, request: ChatCompletionRequest, deployment: DeploymentRecord
    ) -> ChatCompletionResponse:
        prompt = request.messages[-1].content if request.messages else ""
        return ChatCompletionResponse(
            model=request.model,
            deployment_id=deployment.deployment_id,
            routed_hotkey=deployment.hotkey,
            choices=[ChatCompletionChoice(
                index=0,
                message=ChatCompletionMessage(
                    role="assistant",
                    content=f"greencompute-response: {prompt}",
                ),
                finish_reason="stop",
            )],
        )

