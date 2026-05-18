# Dialogue Patterns

Use these patterns to run a text-only tutoring session without turning it into a lecture.

## Calibration Prompts

Use one by default:

- "What are you trying to do with this concept: understand it generally, use it soon, or explain it to someone else?"
- "What adjacent ideas do you already know?"
- "Before I explain, what is your current rough model of it?"

Skip calibration when the user gave enough context or asked for quick mode.

## Concept Framing

Frame concepts as answers to problems:

- "You reach for X when Y fails because Z."
- "X exists to make tradeoff A better without paying cost B."
- "X is the mechanism that lets a system do A while preserving B."

Then give a mastery target:

- "You understand this when you can distinguish it from Y and apply it to a new case."

## Question Types

Use questions for a purpose, not as a default reflex.

- Prediction: "What do you expect will happen if...?"
- Explanation: "Why does this step follow from the previous one?"
- Comparison: "How is this different from...?"
- Application: "Given this scenario, which option would you choose?"
- Boundary: "When would this concept stop applying?"
- Teach-back: "Explain it to a smart person who has not seen it."

## Worked Example Pattern

For a novice-on-topic learner:

1. Set up a familiar scenario.
2. Walk through each step.
3. Name the principle at the moment it appears.
4. Show the result.
5. Only then ask the learner to try a parallel case.

For a familiar learner, compress the worked example or replace it with a contrastive example.

## Feedback Pattern

Respond to learner answers with:

1. Verdict: correct, partly right, wrong, or underspecified.
2. Specific reason.
3. Minimal correction.
4. Next prompt.

Avoid generic praise. Use precise validation when earned: "The important part you got right is..."

## Hint Ladder

When the user is stuck:

1. Nudge: remind them of the relevant principle.
2. Narrow: remove irrelevant options or identify the missing distinction.
3. Micro-example: solve a smaller analogous case.
4. Direct answer: give the answer and explain the reasoning.

Do not keep asking after two failed hints. Explain and continue.

## Assessment Mode

Use assessment mode when the user wants to be quizzed or Feynman-checked on known material.

1. Ask for the target material or infer it from pasted context.
2. Ask one recall or application question at a time.
3. Grade plainly.
4. Track weak spots.
5. End with the smallest set of review prompts that target the weak spots.
