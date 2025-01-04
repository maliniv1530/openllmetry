import logging
import time

from opentelemetry.instrumentation.anthropic.config import Config
from opentelemetry.instrumentation.anthropic.utils import (
    dont_throw,
    error_metrics_attributes,
    count_prompt_tokens_from_request,
    set_span_attribute,
    shared_metrics_attributes,
    should_send_prompts,
)
from opentelemetry.metrics import Counter, Histogram
from opentelemetry.semconv_ai import SpanAttributes
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.trace.span import Span  
from opentelemetry.util.types import Attributes 

logger = logging.getLogger(__name__)


@dont_throw
def _process_response_item(item, complete_response):
    if item.type == "message_start":
        complete_response["model"] = item.message.model
        complete_response["usage"] = dict(item.message.usage)
    elif item.type == "content_block_start":
        index = item.index
        if len(complete_response.get("events")) <= index:
            complete_response["events"].append({"index": index, "text": ""})
    elif item.type == "content_block_delta" and item.delta.type == "text_delta":
        index = item.index
        complete_response.get("events")[index]["text"] += item.delta.text
    elif item.type == "message_delta":
        for event in complete_response.get("events", []):
            event["finish_reason"] = item.delta.stop_reason
        if item.usage:
            if "usage" in complete_response:
                item_output_tokens = dict(item.usage).get("output_tokens", 0)
                existing_output_tokens = complete_response["usage"].get("output_tokens", 0)
                complete_response["usage"]["output_tokens"] = item_output_tokens + existing_output_tokens
            else:
                complete_response["usage"] = dict(item.usage)


def _set_token_usage(
    span,
    complete_response,
    prompt_tokens,
    completion_tokens,
    metric_attributes: dict = {},
    token_histogram: Histogram = None,
    choice_counter: Counter = None,
):
    cache_read_tokens = complete_response.get("usage", {}).get("cache_read_input_tokens", 0)
    cache_creation_tokens = complete_response.get("usage", {}).get("cache_creation_input_tokens", 0)

    input_tokens = prompt_tokens + cache_read_tokens + cache_creation_tokens
    total_tokens = input_tokens + completion_tokens

    set_span_attribute(span, SpanAttributes.LLM_USAGE_PROMPT_TOKENS, input_tokens)
    set_span_attribute(
        span, SpanAttributes.LLM_USAGE_COMPLETION_TOKENS, completion_tokens
    )
    set_span_attribute(span, SpanAttributes.LLM_USAGE_TOTAL_TOKENS, total_tokens)

    set_span_attribute(
        span, SpanAttributes.LLM_RESPONSE_MODEL, complete_response.get("model")
    )
    set_span_attribute(
        span, SpanAttributes.LLM_USAGE_CACHE_READ_INPUT_TOKENS, cache_read_tokens
    )
    set_span_attribute(
        span, SpanAttributes.LLM_USAGE_CACHE_CREATION_INPUT_TOKENS, cache_creation_tokens
    )

    if token_histogram and type(input_tokens) is int and input_tokens >= 0:
        token_histogram.record(
            input_tokens,
            attributes={
                **metric_attributes,
                SpanAttributes.LLM_TOKEN_TYPE: "input",
            },
        )

    if token_histogram and type(completion_tokens) is int and completion_tokens >= 0:
        token_histogram.record(
            completion_tokens,
            attributes={
                **metric_attributes,
                SpanAttributes.LLM_TOKEN_TYPE: "output",
            },
        )

    if type(complete_response.get("events")) is list and choice_counter:
        for event in complete_response.get("events"):
            choice_counter.add(
                1,
                attributes={
                    **metric_attributes,
                    SpanAttributes.LLM_RESPONSE_FINISH_REASON: event.get(
                        "finish_reason"
                    ),
                },
            )

def _emit_completion_event(span: Span, content: str, index: int, finish_reason: Optional[str] = None):
    """Emit a completion event following the new semantic conventions."""
    attributes: Attributes = {
        "messaging.content": content,
        "messaging.index": index,
    }
    if finish_reason:
        attributes["llm.response.finish_reason"] = finish_reason
    span.add_event("completion", attributes=attributes)


def _set_completions(span, events):
    if not span.is_recording() or not events:
        return

    if Config.use_legacy_attributes:
            try:
                for event in events:
                    index = event.get("index")
                    prefix = f"{SpanAttributes.LLM_COMPLETIONS}.{index}"
                    set_span_attribute(
                        span, f"{prefix}.finish_reason", event.get("finish_reason")
                    )
                    set_span_attribute(span, f"{prefix}.content", event.get("text"))
            except Exception as e:
                logger.warning("Failed to set completion attributes, error: %s", str(e))
    else:
        # Added: Emit completion events for streaming responses when not using legacy attributes
        try:
            for i, event in enumerate(events):
                _emit_completion_event(span, event.get("text"), i, event.get("finish_reason"))
        except Exception as e:
            logger.warning("Failed to emit completion event, error: %s", str(e))

@dont_throw
def build_from_streaming_response(
    span,
    response,
    instance,
    start_time,
    token_histogram: Histogram = None,
    choice_counter: Counter = None,
    duration_histogram: Histogram = None,
    exception_counter: Counter = None,
    kwargs: dict = {},
):
    complete_response = {"events": [], "model": "", "usage": {}}
    for item in response:
        try:
            yield item
        except Exception as e:
            attributes = error_metrics_attributes(e)
            if exception_counter:
                exception_counter.add(1, attributes=attributes)
            raise e
        _process_response_item(item, complete_response)

    metric_attributes = shared_metrics_attributes(complete_response)

    if duration_histogram:
        duration = time.time() - start_time
        duration_histogram.record(
            duration,
            attributes=metric_attributes,
        )

    # calculate token usage
    if Config.enrich_token_usage:
        try:
            completion_tokens = -1
            # prompt_usage
            if usage := complete_response.get("usage"):
                prompt_tokens = usage.get("input_tokens", 0)
            else:
                prompt_tokens = count_prompt_tokens_from_request(instance, kwargs)

            # completion_usage
            if usage := complete_response.get("usage"):
                completion_tokens = usage.get("output_tokens", 0)
            else:
                completion_content = ""
                if complete_response.get("events"):
                    model_name = complete_response.get("model") or None
                    for event in complete_response.get("events"):  # type: dict
                        if event.get("text"):
                            completion_content += event.get("text")

                    if model_name:
                        completion_tokens = instance.count_tokens(completion_content)

            _set_token_usage(
                span,
                complete_response,
                prompt_tokens,
                completion_tokens,
                metric_attributes,
                token_histogram,
                choice_counter,
            )
        except Exception as e:
            logger.warning("Failed to set token usage, error: %s", e)

    if should_send_prompts():
        _set_completions(span, complete_response.get("events"))

    span.set_status(Status(StatusCode.OK))
    span.end()


@dont_throw
async def abuild_from_streaming_response(
    span,
    response,
    instance,
    start_time,
    token_histogram: Histogram = None,
    choice_counter: Counter = None,
    duration_histogram: Histogram = None,
    exception_counter: Counter = None,
    kwargs: dict = {},
):
    complete_response = {"events": [], "model": "", "usage": {}}
    async for item in response:
        try:
            yield item
        except Exception as e:
            attributes = error_metrics_attributes(e)
            if exception_counter:
                exception_counter.add(1, attributes=attributes)
            raise e
        _process_response_item(item, complete_response)

    metric_attributes = shared_metrics_attributes(complete_response)

    if duration_histogram:
        duration = time.time() - start_time
        duration_histogram.record(
            duration,
            attributes=metric_attributes,
        )

    # calculate token usage
    if Config.enrich_token_usage:
        try:
            # prompt_usage
            if usage := complete_response.get("usage"):
                prompt_tokens = usage.get("input_tokens", 0)
            else:
                prompt_tokens = count_prompt_tokens_from_request(instance, kwargs)

            # completion_usage
            if usage := complete_response.get("usage"):
                completion_tokens = usage.get("output_tokens", 0)
            else:
                completion_content = ""
                if complete_response.get("events"):
                    model_name = complete_response.get("model") or None
                    for event in complete_response.get("events"):  # type: dict
                        if event.get("text"):
                            completion_content += event.get("text")

                    if model_name:
                        completion_tokens = instance.count_tokens(completion_content)

            _set_token_usage(
                span,
                complete_response,
                prompt_tokens,
                completion_tokens,
                metric_attributes,
                token_histogram,
                choice_counter,
            )
        except Exception as e:
            logger.warning("Failed to set token usage, error: %s", str(e))

    if should_send_prompts():
        _set_completions(span, complete_response.get("events"))

    span.set_status(Status(StatusCode.OK))
    span.end()
