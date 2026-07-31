# Fitness Workout Retrieval Plan

## Goal

Use local embeddings to find stored workout plans from natural-language requests,
including HYROX-oriented training needs. The feature is read-only: selecting a
result displays its full stored workout plan and never schedules, saves, or
modifies a workout.

## Data and retrieval

- Embed the existing `workouts` records: name, category, structure type, and
  description.
- Store vectors locally in DuckDB; create them with the local Ollama embedding
  model. Do not send workout data to an external service.
- Return a short, ranked list with a stable workout ID, date, name, and a
  concise matching excerpt.
- Support semantic requests such as "leg-heavy workout plans," including plans
  that describe squats, lunges, step-ups, sleds, deadlifts, or wall balls
  without using the exact phrase "leg-heavy."

## Interaction

1. The user asks for a workout type or training need.
2. Groundhog shows the best matching plans as a numbered list.
3. The user chooses a number or stable workout ID.
4. Groundhog displays the complete plan stored in that workout record.

HYROX is one retrieval topic among others, not a separate workflow. Questions
such as "find a HYROX-style legs session" should use the same index.

## Guardrails

- Keep exact dates, plan text, and workout metadata grounded in DuckDB results.
- Embeddings rank candidates; they do not invent workout content.
- Do not persist the user's selection or alter any training schedule.
