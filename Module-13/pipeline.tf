resource "aws_codecommit_repository" "website" {
  repository_name = "front_end_website"
  description     = "Repository for the cafe website front end"
}

resource "aws_codepipeline" "website" {
  count          = var.enable_pipeline ? 1 : 0
  name           = "cafe_website_front_end_pipeline"
  role_arn       = var.pipeline_role_arn
  pipeline_type  = "V1"
  execution_mode = "SUPERSEDED"

  artifact_store {
    location = var.artifact_bucket
    type     = "S3"
  }

  stage {
    name = "Source"
    action {
      name             = "Source"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeCommit"
      version          = "1"
      run_order        = 1
      output_artifacts = ["MyApp"]
      configuration = {
        RepositoryName       = aws_codecommit_repository.website.repository_name
        BranchName           = "main"
        PollForSourceChanges = "true"
      }
    }
  }

  stage {
    name = "Deploy"
    action {
      name            = "CafeWebsite"
      category        = "Deploy"
      owner           = "AWS"
      provider        = "S3"
      version         = "1"
      run_order       = 1
      input_artifacts = ["MyApp"]
      configuration = {
        BucketName   = var.site_bucket
        Extract      = "true"
        CacheControl = "max-age=14"
      }
    }
  }
}
