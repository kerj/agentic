function plan() {
  echo "📋 Agile Planning Tool"
  echo ""
  echo "Describe the feature or work you want to plan:"
  echo "(Press Ctrl+D when done, or type on one line)"
  echo ""
  
  # Read multi-line input
  local description=""
  if [[ -t 0 ]]; then
    # Interactive mode
    local line
    while IFS= read -r line; do
      description+="$line"$'\n'
    done
  fi
  
  # Trim trailing newline
  description="${description%$'\n'}"
  
  if [[ -z "$description" ]]; then
    echo "❌ No description provided"
    return 1
  fi
  
  echo ""
  echo "📝 Planning: $description"
  echo ""
  
  # Create output directory
  mkdir -p .claude/plans
  local timestamp=$(date +%Y%m%d-%H%M%S)
  local slug=$(echo "$description" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | cut -c1-30)
  local output_file=".claude/plans/${timestamp}_${slug}.md"
  
  # Phase 1: Ask clarification questions
  echo "🤔 Analyzing and preparing clarification questions..."
  echo ""
  
  local clarification_prompt="You are a product manager gathering requirements.

Read this feature request:
\"\"\"
$description
\"\"\"

Ask 3-5 focused clarification questions to better understand:
- Who are the users?
- What is the scope?
- What are the constraints?
- What are the success criteria?

Output ONLY the questions, one per line, numbered.
Example:
1. Who is the primary user for this feature?
2. Are there any performance requirements?
3. What devices/platforms need to be supported?"

  local questions=$(claude --model $AGENTIC_MODEL --print <<< "$clarification_prompt")
  
  echo "Questions:"
  echo "$questions"
  echo ""
  
  # Get answers
  echo "Please answer these questions:"
  echo "(Type your answer after each question, press Enter. Type 'skip' to skip)"
  echo ""
  
  local answers=""
  local question_num=1
  
  while IFS= read -r question; do
    if [[ -n "$question" && ! "$question" =~ ^[[:space:]]*$ ]]; then
      echo "$question"
      read -r "answer"
      
      if [[ "$answer" != "skip" && -n "$answer" ]]; then
        answers+="Q: $question"$'\n'
        answers+="A: $answer"$'\n\n'
      fi
    fi
  done <<< "$questions"
  
  echo ""
  echo "📋 Generating agile plan..."
  echo ""
  
  # Phase 2: Generate plan
  local planning_prompt="You are an agile product manager creating a detailed work breakdown.

FEATURE REQUEST:
$description

CLARIFICATIONS:
$answers

Create a comprehensive agile plan in markdown format.

Include:
1. Epic title and description
2. User stories (As a [role], I can [action] so that [benefit])
3. Acceptance criteria for each story (Given-When-Then format)
4. Story point estimates (1, 2, 3, 5, 8, 13)
5. Technical tasks breakdown
6. Sprint recommendations
7. Risks and dependencies

Use this structure:

# Epic: [Title]

## Overview
[Description of what we're building and why]

**Business Value:** [Why this matters]

## User Stories

### Story 1: [ID] - [Title]
**As a** [role]  
**I can** [action]  
**So that** [benefit]

**Story Points:** [1-13]  
**Priority:** [High/Medium/Low]

**Acceptance Criteria:**
- Given [context], when [action], then [outcome]
- Given [context], when [action], then [outcome]

**Technical Tasks:**
- [ ] Task 1
- [ ] Task 2

**Notes:** [Any additional context]

---

[Repeat for each story]

## Technical Considerations
- [Architecture decisions]
- [Technical risks]
- [Dependencies]

## Sprint Breakdown

**Sprint 1 (Stories: [IDs])**
- Focus: [What we're achieving]
- Deliverable: [What will be done]

**Sprint 2 (Stories: [IDs])**
- Focus: [What we're achieving]
- Deliverable: [What will be done]

## Definition of Done
- [ ] All acceptance criteria met
- [ ] Code reviewed
- [ ] Tests written and passing
- [ ] Documentation updated

## Risks and Mitigation
| Risk | Impact | Mitigation |
|------|--------|------------|
| [Risk description] | High/Medium/Low | [How to address] |

Output ONLY the markdown. No preamble or explanation."

  # Generate plan
  claude --model $AGENTIC_MODEL --print > "$output_file" <<< "$planning_prompt"
  
  # Check if generated
  if [[ ! -s "$output_file" ]]; then
    echo "❌ Failed to generate plan"
    return 1
  fi
  
  # Clean markdown fences if present
  if grep -q '```' "$output_file"; then
    sed -i.bak '/```markdown/d; /```$/d' "$output_file"
    rm -f "$output_file.bak"
  fi
  
  echo "✅ Plan created: $output_file"
  echo ""
  
  # Preview
  echo "Preview:"
  echo "─────────────────────────────────────"
  head -30 "$output_file"
  local total_lines=$(wc -l < "$output_file" | tr -d ' ')
  if [[ $total_lines -gt 30 ]]; then
    echo "..."
    echo "($((total_lines - 30)) more lines)"
  fi
  echo "─────────────────────────────────────"
  echo ""
  
  # Symlink to latest
  rm -f .claude/plans/latest.md
  ln -s "$(basename "$output_file")" .claude/plans/latest.md
  
  # Options
  echo "Options:"
  echo "  1. Open in editor: ${EDITOR:-nano} $output_file"
  echo "  2. View full plan: cat $output_file"
  echo "  3. Copy to clipboard: pbcopy < $output_file"
  echo ""
  
  read -p "Open in editor now? (y/n) " open_editor
  if [[ "$open_editor" =~ ^[Yy]$ ]]; then
    ${EDITOR:-nano} "$output_file"
  fi
}