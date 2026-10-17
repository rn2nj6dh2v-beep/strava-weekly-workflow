"""Point d'entrée du worker Mistral Workflow."""
import mistralai.workflows as workflows

from src.workflows.strava_weekly import StravaWeeklyWorkflow


def main() -> None:
    workflows.run_worker([StravaWeeklyWorkflow])


if __name__ == "__main__":
    main()
