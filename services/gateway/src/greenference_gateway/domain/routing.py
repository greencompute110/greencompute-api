from greenference_protocol import ChatCompletionRequest, ChatCompletionResponse, DeploymentRecord


class NoReadyDeploymentError(RuntimeError):
    pass


class InferenceRouter:
    def render_chat_response(
        self, request: ChatCompletionRequest, deployment: DeploymentRecord
    ) -> ChatCompletionResponse:
        prompt = request.messages[-1].content if request.messages else ""
        return ChatCompletionResponse(
            model=request.model,
            content=f"greenference-response: {prompt}",
            deployment_id=deployment.deployment_id,
            routed_hotkey=deployment.hotkey,
        )

