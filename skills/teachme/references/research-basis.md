# Research Basis

This skill uses a practical synthesis of learning-science findings and AI-tutoring design patterns.

## High-Utility Techniques

Retrieval practice and distributed practice are among the strongest general-purpose learning techniques. Prefer generative prompts where the learner produces an answer over recognition-only checks.

Sources:

- Dunlosky et al., "Improving Students' Learning With Effective Learning Techniques" (2013): https://pubmed.ncbi.nlm.nih.gov/26173288/
- Roediger and Karpicke, "Test-Enhanced Learning" (2006): https://journals.sagepub.com/doi/10.1111/j.1467-9280.2006.01693.x

## Cognitive Load And Worked Examples

Worked examples reduce unnecessary load for novices. Fade them as the learner gains schema, because too much support can become redundant for more advanced learners.

Sources:

- van Gog, Paas, and Sweller, "Cognitive Load Theory" discussion of worked examples and practice: https://link.springer.com/article/10.1007/s10648-010-9145-4
- Renkl, Atkinson, and Grosse, "How Fading Worked Solution Steps Works" (2004): https://link.springer.com/article/10.1023/B%3ATRUC.0000021815.74806.f6

## Active, Constructive, Interactive Learning

The ICAP framework argues that learning activities become more effective as learners move from passive reception toward active, constructive, and interactive engagement. In text tutoring, that means asking the learner to predict, apply, explain, compare, and teach back.

Source:

- Chi and Wylie, "The ICAP Framework" (2014): https://www.tandfonline.com/doi/full/10.1080/00461520.2014.965823

## Dialogue Tutoring

Good tutoring dialogue combines prompts, hints, feedback, misconception correction, and summaries. Questioning alone is not enough; the tutor must infer the learner state and adapt.

Sources:

- Graesser et al., "AutoTutor" research overview: https://digitalcommons.memphis.edu/facpubs/7458/
- Kulik and Fletcher, "Effectiveness of Intelligent Tutoring Systems" (2016): https://journals.sagepub.com/doi/10.3102/0034654315581420

## Desirable Difficulties

Challenge is useful when it improves retrieval, discrimination, or transfer without overwhelming the learner. Use prediction, recall, and interleaving, but pair them with enough scaffolding to stay productive.

Source:

- Bjork and Kroll, "Desirable Difficulties in Vocabulary Learning" (2015): https://pmc.ncbi.nlm.nih.gov/articles/PMC4888598/

## AI Tutor Prior Art

Useful patterns from modern AI tutor products and open-source examples include calibration, Socratic prompts, immediate feedback, study sheets, and spaced review prompts. Treat these as design patterns, not definitive evidence.

Examples:

- OpenAI Study Mode: https://openai.com/index/chatgpt-study-mode/
- OpenAI Socratic tutor prompt example: https://platform.openai.com/docs/examples/default-socratic-tutor
- openai-study-mode-clone: https://github.com/DevHammad0/openai-study-mode-clone
- LlamaTutor: https://github.com/Nutlope/llamatutor
- Gareth Manning's claude-education-skills: https://github.com/garethmanning/claude-education-skills
