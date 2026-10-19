//! Per-connection `previous_response_id` continuation for inbound Responses
//! WebSocket sessions.

use std::sync::{Arc, Mutex};

use serde_json::Value;

use crate::{
    adapters::responses::codex_continuation::{
        build_transcript, extends_transcript, StoredContinuation,
    },
    codex_endpoint::frame::{terminal_status_from_type, TerminalStatus},
    config::Config,
};

/// Per-connection continuation captured from the last completed turn, so an
/// incremental `response.create` frame (`previous_response_id` + input delta)
/// can be expanded to the full input the HTTP upstream requires: the ChatGPT
/// HTTP backend rejects `previous_response_id` outright
/// (`Unsupported parameter: previous_response_id`), unlike the WebSocket
/// backend the client is speaking. Lives only for one socket, like the
/// client's own continuation state.
pub(super) type SharedContinuation = Arc<Mutex<Option<StoredContinuation>>>;

/// Whether a turn for `model` is served by a ChatGPT backend, resolved the way
/// `codex_endpoint::forward_turn` routes it: a model with no
/// `[[server.codex_endpoint.routes]]` entry (or a frame with no readable
/// model) goes to the endpoint's own ChatGPT/Codex pool; a routed one goes to
/// its route's provider.
pub(super) fn targets_chatgpt_backend(config: &Config, model: Option<&str>) -> bool {
    let route = config
        .server
        .codex_endpoint
        .as_ref()
        .zip(model)
        .and_then(|(endpoint, model)| endpoint.route_for(model));
    route.is_none_or(|route| config.is_chatgpt_backend(&route.provider))
}

/// Prepare a `response.create` body for the ChatGPT HTTP upstream: drop
/// `previous_response_id` (rejected there) and, when it names this
/// connection's stored response and the input is only a delta, expand the
/// input to the full transcript. An input that already starts with the stored
/// transcript (the client sent the full history with the id as a hint)
/// forwards unchanged apart from the dropped field, whatever else changed.
/// Returns the full logical input the prepared body carries.
pub(super) fn prepare_body(raw_json: &mut Value, continuation: &SharedContinuation) -> Vec<Value> {
    let Some(obj) = raw_json.as_object_mut() else {
        return Vec::new();
    };
    let previous = obj
        .remove("previous_response_id")
        .and_then(|value| value.as_str().map(ToOwned::to_owned));
    let input = input_items(raw_json);
    let Some(full) = expand(continuation, previous.as_deref(), &input) else {
        return input;
    };
    if let Some(obj) = raw_json.as_object_mut() {
        obj.insert("input".to_string(), Value::Array(full.clone()));
    }
    full
}

/// The full logical input of a frame forwarded unchanged to a routed
/// Responses API, which honours `previous_response_id` itself. The body keeps
/// the id and its delta; only the recorded continuation needs the expansion,
/// so a later turn the connection sends to a ChatGPT backend still carries the
/// whole history.
pub(super) fn routed_logical_input(
    raw_json: &Value,
    continuation: &SharedContinuation,
) -> Vec<Value> {
    let input = input_items(raw_json);
    let previous = raw_json.get("previous_response_id").and_then(Value::as_str);
    expand(continuation, previous, &input).unwrap_or(input)
}

/// The stored transcript followed by `input`, when `previous` names the stored
/// response and `input` is a delta after it rather than the full history.
fn expand(
    continuation: &SharedContinuation,
    previous: Option<&str>,
    input: &[Value],
) -> Option<Vec<Value>> {
    let previous = previous?;
    let guard = continuation.lock().ok()?;
    let stored = guard
        .as_ref()
        .filter(|stored| stored.response_id == previous)?;
    if extends_transcript(&stored.transcript, input) {
        return None;
    }
    Some(build_transcript(&stored.transcript, input))
}

/// A body's `input` as items. The Responses API also accepts a plain string,
/// which is one user message; normalizing it keeps that prompt in the
/// transcript a later delta is expanded against.
fn input_items(raw_json: &Value) -> Vec<Value> {
    match raw_json.get("input") {
        Some(Value::Array(items)) => items.clone(),
        Some(Value::String(text)) => vec![serde_json::json!({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        })],
        _ => Vec::new(),
    }
}

/// Records one turn's upstream events against the connection continuation.
/// Like `codex_ws::capture_continuation`, the response id is captured from
/// response-level events (nested `response.id`, or a top-level `id` on e.g.
/// `response.done`) and output items from `response.output_item.done`, since
/// the terminal event alone may carry neither.
pub(super) struct TurnRecorder {
    continuation: SharedContinuation,
    /// The full logical input the turn's body carries after expansion, and
    /// the signature of its non-input fields.
    logical_input: Vec<Value>,
    body_signature: String,
    response_id: Option<String>,
    output_items: Vec<Value>,
}

impl TurnRecorder {
    pub(super) fn new(
        continuation: SharedContinuation,
        logical_input: Vec<Value>,
        body_signature: String,
    ) -> Self {
        Self {
            continuation,
            logical_input,
            body_signature,
            response_id: None,
            output_items: Vec::new(),
        }
    }

    /// Observe one upstream event `payload` of type `event_type`, recording
    /// the continuation when it is terminal.
    pub(super) fn observe(&mut self, event_type: &str, payload: &str) {
        let response_level = matches!(
            event_type,
            "response.created" | "response.in_progress" | "response.completed" | "response.done"
        );
        let item_done = event_type == "response.output_item.done";
        let terminal = terminal_status_from_type(event_type);
        if !response_level && !item_done && terminal.is_none() {
            return;
        }
        let value = serde_json::from_str::<Value>(payload).ok();
        if let Some(value) = &value {
            if response_level {
                if let Some(id) = value
                    .pointer("/response/id")
                    .or_else(|| value.get("id"))
                    .and_then(Value::as_str)
                    .filter(|id| !id.is_empty())
                {
                    self.response_id = Some(id.to_string());
                }
            }
            if item_done {
                if let Some(item) = value.get("item") {
                    self.output_items.push(item.clone());
                }
            }
        }
        if let Some(terminal) = terminal {
            self.record(terminal, value.as_ref());
        }
    }

    /// A completed response becomes the prefix the next incremental frame
    /// extends; a failed or incomplete one (or one without an id) clears it,
    /// since the backend state the client would name no longer matches
    /// anything shunt can expand from.
    fn record(&self, terminal: TerminalStatus, payload: Option<&Value>) {
        let Ok(mut guard) = self.continuation.lock() else {
            return;
        };
        let id = self
            .response_id
            .as_ref()
            .filter(|_| terminal == TerminalStatus::Completed);
        let Some(id) = id else {
            *guard = None;
            return;
        };
        let output = payload
            .and_then(|payload| payload.pointer("/response/output"))
            .and_then(Value::as_array)
            .filter(|output| !output.is_empty())
            .unwrap_or(&self.output_items);
        *guard = Some(StoredContinuation {
            response_id: id.clone(),
            signature: self.body_signature.clone(),
            transcript: build_transcript(&self.logical_input, output),
            turn_state: None,
        });
    }
}
