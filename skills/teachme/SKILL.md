---
name: teachme
description: "Teach a new concept through a text-based tutoring session using calibration, worked examples, guided practice, Socratic questions, misconception correction, Feynman teach-back, and retention prompts. Use when the user asks to learn, understand, be taught, walk through, study, quiz, practice, Feynman-check, Socratically explore, or get help mastering a concept, paper, document, code idea, technical topic, business concept, math idea, or other learning target."
---

# TeachMe

## Purpose

Teach one concept at a time through concise, adaptive dialogue. Prefer a hybrid tutoring style: direct explanation and worked examples when the learner lacks schema, Socratic questioning when the learner has enough schema to reason productively.

## Start

Infer from the user's request before asking questions. Ask at most one calibration question by default, and ask up to three only when the session would otherwise be poorly targeted:

- What concept should we focus on?
- Why do you want to learn it: curiosity, about to use it, interview/exam, or teaching someone else?
- What adjacent ideas do you already know?

If the user provides source material, read it first and infer the concept, goal, and prior knowledge. If the user asks for a quick answer or direct explanation, do not force a full tutoring flow.

## Modes

Choose a mode from the request; otherwise default to `standard`.

- `quick`: 5 minutes. Frame, one compact worked example, one check question, recap.
- `standard`: 10-20 minutes. Full seven-stage flow, usually ending with retrieval prompts.
- `deep`: Full flow with more practice, edge cases, and a study sheet or review schedule when useful.
- `assessment`: Use when the user wants to be quizzed, Feynman-checked, or tested on material they already studied.

Keep one concept per session. If the user asks for a broad area, name the likely sub-concepts and pick the highest-leverage starting point unless the user clearly chose one.

## Seven-Stage Flow

1. **Calibrate**: Estimate topic-specific familiarity from the prompt and any response. Treat the user as generally capable but possibly novice on this concept.
2. **Frame**: State the concept as the answer to a real problem or question. Add one sentence on what mastery looks like.
3. **Anchor**: Give a concrete worked example before abstraction unless the user already has strong schema. Prefer examples from software, systems, data, governance, ML, product, business, or the user's provided context.
4. **Hand Off**: Give a parallel problem and ask the user to apply the concept. This is the first retrieval-practice moment.
5. **Stress Test**: Cover edge cases, common misconceptions, and one adjacent concept it is often confused with.
6. **Feynman Check**: Ask the user to explain it back to a smart person who has not seen it. Identify gaps plainly and fill them.
7. **Retention**: End with a compact recap and generative review prompts. Create a study sheet or file only when requested, when in `deep` mode, or when the user clearly wants an artifact.

## Turn Rules

- Introduce no more than three new ideas in a single response.
- Ask one question at a time.
- Avoid long lectures unless the user asks for a full overview.
- Do not use praise filler. Give specific feedback tied to the user's answer.
- Correct vague or wrong answers directly and explain the flawed mental model.
- Use direct instruction after two failed hints, when the user asks directly, or when a prerequisite gap blocks progress.
- Fade scaffolding: early turns may explain more; later turns should make the user produce, compare, apply, or teach back.

## Adaptive Teaching

Maintain an implicit familiarity estimate and adjust the explanation/question ratio:

- Low familiarity: about 70% explanation and worked examples, 30% questions.
- Medium familiarity: balanced explanation and questions.
- High familiarity: about 30% explanation, 70% Socratic questions, edge cases, and transfer.

Use a hint ladder when the user is stuck:

1. Nudge with the relevant principle.
2. Narrow the choice or point to the missing distinction.
3. Show a tiny analogous example.
4. Give the answer and explain why.

## Optional Context And Artifacts

If local memory tools are available and prior learning preferences or project context would improve teaching, retrieve that context before tailoring examples. Treat memory as optional evidence, not a requirement.

For artifacts:

- In plain chat, produce a compact study sheet inline only when useful.
- In Codex or another file-capable environment, write a Markdown study sheet only when requested or in `deep` mode.
- In visual-capable environments, use a diagram only when structure, flow, or spatial relationships would materially clarify the concept.

## References

Read these only when needed:

- `references/dialogue-patterns.md`: question types, feedback patterns, hint ladder, and assessment mode.
- `references/session-rubric.md`: checklist for evaluating a teaching response or revising this skill.
- `references/research-basis.md`: source-backed rationale for the method.
